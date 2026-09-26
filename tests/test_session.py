"""Session / EventMemory 压缩与会话生命周期的测试：llm 注入可自动分发的 FakeLLM。

覆盖验收点：
  1. 10 条消息 compress -> 占位符；retrieve_full 能找回事件原文。
  2. begin -> add_message -> end_session()：新 add_event 不受影响，
     原 pending 内容已可在 retrieve_full 中检索到。
  3. end_session(seal=False) 不落盘，pending 清空，检索不到。
  4. 重复 compress 相同内容，事件总数不变（去重生效）。
  5. Session 基础语义（pending 首条 = task、estimate_tokens 估算规则）。
"""

from __future__ import annotations

import re

from event_memory.extractor import _SPLIT_RE
from event_memory.memory import EventMemory, Session


# ---------------------------------------------------------------------------
# 辅助：可自动分发行为的 FakeLLM（按 system 提示词分发 segment / extract / judge）
# ---------------------------------------------------------------------------

class FakeLLM:
    """注入 Extractor 的假客户端：按 system 提示词自动分发三项 LLM 能力。

    - segment：从 __HISTORY__ 块解析每行"角色: 内容"，每行作为一个事件原文。
    - extract：从 __EVENT__ 正文切词，首个词作 hard，其余作 soft。
    - judge：默认把全部候选判为相关（permissive）；传 relevant_override 可指定子集。
    """

    KNOWN_ROLES = ("user:", "assistant:", "system:", "human:", "ai:", "bot:")

    def __init__(self, *, relevant_override=None):
        self.relevant_override = relevant_override
        self.calls = []

    def chat_json(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
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
        ids = self._extract_candidate_ids(user)
        if self.relevant_override is not None:
            relevant = [i for i in self.relevant_override if i in ids]
        else:
            relevant = list(ids)
        return {"relevant": relevant}

    @staticmethod
    def _event_text(user: str) -> str:
        for marker in ("事件原文：", "事件原文:"):
            parts = user.split(marker, 1)
            if len(parts) == 2:
                return parts[1].split("输出要求", 1)[0].strip()
        return ""

    @staticmethod
    def _extract_candidate_ids(user: str) -> list[str]:
        return [m.group(1) for m in re.finditer(r'"event_id"\s*:\s*"([^"]*)"', user)]


def _build(tmp_path, **kwargs):
    return EventMemory(tmp_path, llm=FakeLLM(**kwargs))


# ---------------------------------------------------------------------------
# 验收 1：10 条消息 compress -> 占位符；retrieve_full 找回原文
# ---------------------------------------------------------------------------

def test_compress_placeholder_and_retrieve_full(tmp_path):
    m = _build(tmp_path)

    history = [
        # 空格分词：FakeLLM 提词时 "编号i" 成为独立硬关键词，便于按编号精确检索
        {"role": "user", "content": f"编号{i} 鹈鹕骑行活动 详情{i}"}
        for i in range(1, 11)
    ]

    placeholder = m.compress(history)
    assert placeholder == "[事件已归档, 可用关键词检索]"
    assert m.stats()["events"] == 10  # 10 条独立内容各落一个事件

    # 用唯一编号检索，能找回对应事件原文
    mounts = m.retrieve_full(query="编号5")
    assert len(mounts) == 1
    assert "编号5 鹈鹕骑行活动 详情5" in mounts[0].content


# ---------------------------------------------------------------------------
# 验收 2：begin -> add_message -> end_session()；新 add_event 不受影响，原内容可检索
# ---------------------------------------------------------------------------

def test_begin_add_end_session_seal_stores_pending(tmp_path):
    m = _build(tmp_path)

    m.add_event("会话前的独立事件", ["独立", "事件"], [], summary="独立")

    session = m.begin_session("编写测试内容", "sess-001")
    session.add_message("user", "user 输入内容")
    session.add_message("assistant", "助手回复内容")
    session.end_session()

    # 原 pending 内容已可在 retrieve_full 中检索到
    mounts = m.retrieve_full(query="助手回复内容")
    assert any("助手回复内容" in mc.content for mc in mounts)

    # 新 add_event 不受影响
    m.add_event("会话后的独立事件", ["会话后的独立事件"], [], summary="独立2")
    mounts_after = m.retrieve_full(query="会话后的独立事件", llm_query_extract=False)
    assert any("会话后的独立事件" in mc.content for mc in mounts_after)


# ---------------------------------------------------------------------------
# 验收 3：end_session(seal=False) 不落盘，pending 清空，检索不到
# ---------------------------------------------------------------------------

def test_end_session_no_seal_does_not_store(tmp_path):
    m = _build(tmp_path)

    session = m.begin_session("不落盘任务", "sess-002")
    session.add_message("user", "应该被丢弃的内容")
    session.end_session(seal=False)

    assert session.pending == []
    assert m.stats()["events"] == 0
    assert m.retrieve_full(query="应该被丢弃的内容", llm_query_extract=False) == []


# ---------------------------------------------------------------------------
# 验收 4：重复 compress 相同内容，事件总数不变（去重）
# ---------------------------------------------------------------------------

def test_repeated_compress_is_deduplicated(tmp_path):
    m = _build(tmp_path)

    history = [{"role": "user", "content": "唯一事件内容"}]
    m.compress(history)
    count1 = m.stats()["events"]
    m.compress(history)
    count2 = m.stats()["events"]

    assert count1 == count2 == 1


# ---------------------------------------------------------------------------
# 验收 5：Session 基础语义
# ---------------------------------------------------------------------------

def test_session_first_message_is_task_and_estimate_tokens(tmp_path):
    m = _build(tmp_path)
    session = m.begin_session("初始任务文本", "sess-005")

    assert session.session_id == "sess-005"
    # task 作为第一条消息进入 pending
    assert session.pending[0] == {"role": "user", "content": "初始任务文本"}
    session.add_message("assistant", "回复 1")
    session.add_message("user", "回复 2")

    # 估算规则：中文字符*1 + 其他字符/4，向上取整（只算内容不算 role）
    # 初始任务文本(6中) + 回复 1(2中+2/4) + 回复 2(2中+2/4)
    expected = 6 + (2 + 2 / 4) + (2 + 2 / 4)
    assert session.estimate_tokens() == int(expected) + (1 if expected % 1 else 0)


def test_session_estimate_tokens_mixed(tmp_path):
    m = _build(tmp_path)
    session = m.begin_session("中文英文123", "sess-x")
    tokens = session.estimate_tokens()
    # 中文英文(4中) + 123(3非中)/4
    expected = 4 + 3 / 4
    assert tokens == int(expected) + (1 if expected % 1 else 0)
