"""AgentMemoryBridge：event_memory 嵌入自建 agent 编排层的粘合层。

核心原则：上下文由 agent 编排层管理，模型不管理——模型只消费被喂进来的内容；
切分、挂载、落盘、开关会话全部由本桥接层（配合 EventMemory）执行。

agent 主循环约定：
    任务到来:
        bridge = AgentMemoryBridge(memory, window_tokens)
        report = bridge.on_task_start(task, session_id)   # 记忆附加段 + 挂载片段
        把 report.memory_prompt 附加到 system prompt
    每轮:
        result = bridge.on_turn(role, content)            # 记录输入输出
        if result.compressed:                             # 超阈值已压缩
            用 result.placeholder 替换已归档的历史
    模型请求记忆 / 关键词 miss:
        text = bridge.on_memory_request(query)            # 检索结果或 browse 列表
        bridge.on_pick(event_no)                          # 模型指定序号后挂载
    任务完成:
        bridge.on_task_end()                              # seal 落盘，上下文清零

所有 memory 异常一律吞掉并降级（记忆系统故障不能拖死 agent 主流程），
最后错误可通过 last_error 查看。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from event_memory.memory import EventMemory, Session, COMPRESS_PLACEHOLDER
from event_memory.models import MountedEvent


@dataclass
class TaskStartReport:
    """on_task_start 的返回：记忆附加段 + 挂载片段列表。"""

    memory_prompt: str = ""          # 拼好的记忆声明段，附加到 system prompt
    mounted: list[MountedEvent] = field(default_factory=list)


@dataclass
class TurnResult:
    """on_turn 的返回：是否触发压缩及占位符信息。"""

    compressed: bool = False
    placeholder: str = ""
    tokens: int = 0                  # 未触发压缩时为当前 pending 的估算 token


class AgentMemoryBridge:
    """把 EventMemory 接进 agent 主循环的桥接层，全程异常降级。"""

    def __init__(self, memory: EventMemory, window_tokens: int,
                 compress_threshold: float = 0.8) -> None:
        self._memory = memory
        self._window_tokens = int(window_tokens)
        self._threshold = float(compress_threshold)
        self._session: Session | None = None
        self._last_error: str = ""

    # ---- 任务生命周期 ----

    def on_task_start(self, task: str, session_id: str) -> TaskStartReport:
        """任务到来：检索相关历史并挂载，开新会话。"""
        try:
            mounted = self._memory.retrieve_full(task)
        except Exception as exc:  # 记忆故障降级为无记忆
            self._record(exc)
            mounted = []

        if mounted:
            body = "\n\n".join(m.content for m in mounted)
            prompt = (
                "[工作区记忆] 以下是按当前任务检索到的历史事件，"
                "它们不是当前对话流的一部分：\n"
                + body
                + "\n[记忆结束]"
            )
        else:
            prompt = "[工作区记忆] 未检索到与当前任务相关的记忆。"

        try:
            self._session = self._memory.begin_session(task, session_id)
        except Exception as exc:
            self._record(exc)
            self._session = None

        return TaskStartReport(memory_prompt=prompt, mounted=list(mounted))

    # ---- 每轮 ----

    def on_turn(self, role: str, content: str) -> TurnResult:
        """记录每轮输入/输出；pending 估算 token 超阈值时触发压缩。"""
        if self._session is None:
            return TurnResult()
        try:
            self._session.add_message(role, content)
            tokens = self._session.estimate_tokens()
            limit = self._window_tokens * self._threshold
            if tokens > limit:
                placeholder = self._memory.compress(self._session.pending)
                self._session.pending = []  # 已归档，避免 end_session 重复落盘占位符
                return TurnResult(compressed=True, placeholder=placeholder)
            return TurnResult(tokens=tokens)
        except Exception as exc:
            self._record(exc)
            return TurnResult()

    # ---- 记忆请求 ----

    def on_memory_request(self, query: str) -> str:
        """模型请求记忆：检索命中返回挂载文本；miss 返回 browse 列表供挑序号。"""
        try:
            mounted = self._memory.retrieve_full(query)
            if mounted:
                return "\n\n".join(m.content for m in mounted)
            listing = self._memory.browse()
            if listing:
                return (
                    "未检索到直接相关的记忆。以下是最近事件（时间坐标系），"
                    "可指定序号挂载：\n" + listing
                )
            return "无相关记忆。"
        except Exception as exc:
            self._record(exc)
            return ""

    def on_pick(self, event_no: int) -> str:
        """模型在 browse 列表中指定序号后，挂载对应事件原文。"""
        try:
            return self._memory.pick(event_no).content
        except Exception as exc:
            self._record(exc)
            return ""

    # ---- 任务结束 ----

    def on_task_end(self) -> None:
        """任务完成：无条件 seal 落盘并关闭会话，上下文清零。"""
        if self._session is None:
            return
        try:
            self._session.end_session(seal=True)
        except Exception as exc:
            self._record(exc)
        finally:
            self._session = None

    # ---- 辅助 ----

    def _record(self, exc: Exception) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"

    @property
    def last_error(self) -> str:
        return self._last_error


__all__ = [
    "AgentMemoryBridge",
    "TaskStartReport",
    "TurnResult",
    "COMPRESS_PLACEHOLDER",
    "MountedEvent",
]
