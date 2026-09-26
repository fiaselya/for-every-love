"""AgentMemoryBridge 集成层测试。

覆盖验收点：
  1. on_task_start：有记忆时产出含挂载片段的提示段；无记忆时产出"未检索到"声明。
  2. on_turn 累积到阈值触发压缩并返回占位符；未超阈值返回 token 数。
  3. on_task_end 后，新 bridge 的 on_task_start 能检索到上一任务的内容（跨会话持久化）。
  4. 注入会抛异常的假 memory：bridge 不抛异常、优雅降级，last_error 有记录。
  5. on_memory_request：命中返回挂载文本；miss 返回 browse 列表；on_pick 挂回原文。
"""

from __future__ import annotations

import re

from event_memory.extractor import _SPLIT_RE
from event_memory.integration import AgentMemoryBridge
from event_memory.memory import EventMemory, COMPRESS_PLACEHOLDER


# ---------------------------------------------------------------------------
# FakeLLM：按 system 提示词分发 segment / extract / judge（与 test_session 同机制）
# ---------------------------------------------------------------------------

class FakeLLM:
    KNOWN_ROLES = ("user:", "assistant:", "system:", "human:", "ai:", "bot:")

    def chat_json(self, system: str, user: str) -> dict:
        if "分段" in system:
            return self._segment(user)
        if "相关" in system or "判断" in system:
            return self._judge(user)
        return self._extract(user)

    def _segment(self, user: str) -> dict:
        block = user.split("输出要求", 1)[0]
        if "历史对话" in block:
            block = block.split("历史对话", 1)[1]
        events = []
        for line in block.split("\n"):
            line = line.strip()
            if not line or not line.startswith(self.KNOWN_ROLES):
                continue
            content = line.split(":", 1)[1].strip()
            if content:
                events.append({"text": content})
        return {"events": events}

    def _extract(self, user: str) -> dict:
        text = self._event_text(user)
        tokens = [t for t in _SPLIT_RE.split(text) if t]
        return {"hard": tokens[:1], "soft": tokens[1:9], "summary": text[:50]}

    def _judge(self, user: str) -> dict:
        ids = [m.group(1) for m in re.finditer(r'"event_id"\s*:\s*"([^"]*)"', user)]
        return {"relevant": ids}

    @staticmethod
    def _event_text(user: str) -> str:
        for marker in ("事件原文：", "事件原文:"):
            parts = user.split(marker, 1)
            if len(parts) == 2:
                return parts[1].split("输出要求", 1)[0].strip()
        return ""


class BrokenMemory:
    """所有方法都抛异常的假 memory，用于验证桥接层降级。"""

    def retrieve_full(self, *a, **kw):
        raise RuntimeError("retrieve boom")

    def begin_session(self, *a, **kw):
        raise RuntimeError("session boom")

    def compress(self, *a, **kw):
        raise RuntimeError("compress boom")

    def browse(self, *a, **kw):
        raise RuntimeError("browse boom")

    def pick(self, *a, **kw):
        raise RuntimeError("pick boom")


# ---------------------------------------------------------------------------
# 验收 1：on_task_start 有/无记忆
# ---------------------------------------------------------------------------

def test_task_start_with_memory_includes_mounted(tmp_path):
    m = EventMemory(tmp_path, llm=FakeLLM())
    m.add_event("鹈鹕骑车历史事件原文", ["鹈鹕"], [], summary="鹈鹕历史")

    bridge = AgentMemoryBridge(m, window_tokens=10_000)
    report = bridge.on_task_start("鹈鹕 新任务", "s1")

    assert len(report.mounted) == 1
    assert "鹈鹕骑车历史事件原文" in report.memory_prompt
    assert report.memory_prompt.startswith("[工作区记忆]")
    assert "[记忆结束]" in report.memory_prompt


def test_task_start_without_memory_states_absence(tmp_path):
    m = EventMemory(tmp_path, llm=FakeLLM())
    bridge = AgentMemoryBridge(m, window_tokens=10_000)
    report = bridge.on_task_start("全新任务", "s1")
    assert report.mounted == []
    assert "未检索到" in report.memory_prompt


# ---------------------------------------------------------------------------
# 验收 2：on_turn 阈值触发压缩
# ---------------------------------------------------------------------------

def test_turn_threshold_triggers_compression(tmp_path):
    m = EventMemory(tmp_path, llm=FakeLLM())
    bridge = AgentMemoryBridge(m, window_tokens=50, compress_threshold=0.8)  # 阈值 40
    bridge.on_task_start("编号1 任务开始", "s1")  # 约 7 token

    r1 = bridge.on_turn("assistant", "短")  # 未超阈值
    assert r1.compressed is False
    assert r1.tokens > 0

    r2 = bridge.on_turn("assistant", "长" * 35)  # 7 + 35 = 42 > 40
    assert r2.compressed is True
    assert r2.placeholder == COMPRESS_PLACEHOLDER
    assert m.stats()["events"] >= 1  # pending 已落盘

    # 压缩后 pending 清空，继续对话不再立刻触发
    r3 = bridge.on_turn("user", "短")
    assert r3.compressed is False


def test_task_end_does_not_double_store_placeholder(tmp_path):
    m = EventMemory(tmp_path, llm=FakeLLM())
    bridge = AgentMemoryBridge(m, window_tokens=50, compress_threshold=0.8)
    bridge.on_task_start("编号1 任务开始", "s1")
    bridge.on_turn("assistant", "长" * 35)  # 触发压缩
    count_after_compress = m.stats()["events"]
    bridge.on_task_end()  # pending 已空，不应把占位符再存成事件
    assert m.stats()["events"] == count_after_compress


# ---------------------------------------------------------------------------
# 验收 3：任务结束 -> 新任务可检索到上一任务内容
# ---------------------------------------------------------------------------

def test_task_end_then_new_task_retrieves_previous(tmp_path):
    m = EventMemory(tmp_path, llm=FakeLLM())

    b1 = AgentMemoryBridge(m, window_tokens=10_000)
    b1.on_task_start("编号1 初始任务", "s1")
    b1.on_turn("assistant", "编号2 内容甲 详述")
    b1.on_task_end()

    b2 = AgentMemoryBridge(m, window_tokens=10_000)
    report = b2.on_task_start("编号2 继续任务", "s2")
    assert len(report.mounted) == 1
    assert "编号2 内容甲 详述" in report.memory_prompt


# ---------------------------------------------------------------------------
# 验收 4：故障降级
# ---------------------------------------------------------------------------

def test_broken_memory_degrades_gracefully(tmp_path):
    bridge = AgentMemoryBridge(BrokenMemory(), window_tokens=10_000)

    report = bridge.on_task_start("任意任务", "s1")  # 不应抛异常
    assert "未检索到" in report.memory_prompt
    assert report.mounted == []

    r = bridge.on_turn("user", "内容")  # 不应抛异常
    assert r.compressed is False

    assert bridge.on_memory_request("查询") == ""
    assert bridge.on_pick(1) == ""
    bridge.on_task_end()  # 不应抛异常
    assert bridge.last_error != ""


# ---------------------------------------------------------------------------
# 验收 5：on_memory_request 命中 / 兜底 / on_pick
# ---------------------------------------------------------------------------

def test_memory_request_hit_and_browse_fallback(tmp_path):
    m = EventMemory(tmp_path, llm=None)
    m.add_event("鹈鹕骑车动画完成原文", ["鹈鹕"], [], summary="鹈鹕动画")
    bridge = AgentMemoryBridge(m, window_tokens=10_000)

    # 命中：返回挂载文本
    hit = bridge.on_memory_request("鹈鹕")
    assert "鹈鹕骑车动画完成原文" in hit

    # miss：返回 browse 列表
    miss = bridge.on_memory_request("完全无关的查询词")
    assert "时间坐标系" in miss
    assert "#1" in miss

    # on_pick 按序号挂回原文
    picked = bridge.on_pick(1)
    assert "鹈鹕骑车动画完成原文" in picked
