"""event_memory 数据模型（纯 dataclass / 枚举，无逻辑）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum


class EventStatus(IntEnum):
    """事件状态。"""

    ACTIVE = 0  # 进行中
    SEALED = 1  # 已完成
    ARCHIVED = 2  # 冷归档


# 状态 -> 挂载文本中的中文展示
_STATUS_LABEL = {
    EventStatus.ACTIVE: "进行中",
    EventStatus.SEALED: "已完成",
    EventStatus.ARCHIVED: "冷归档",
}


@dataclass
class EventMeta:
    """事件元信息。事件内容不可变，仅靠此元信息检索。"""

    event_id: str  # blake2b hex，16 字符
    created_at: int  # unix 时间戳
    workspace: int = 0
    status: EventStatus = EventStatus.SEALED
    hard_keys: list[str] = field(default_factory=list)  # 硬关键词：检索时 AND 过滤
    soft_keys: list[str] = field(default_factory=list)  # 软关键词：只用于打分排序
    summary: str = ""  # 一句话摘要，二次判断用


@dataclass
class Candidate:
    """检索候选：事件 id + 匹配分。"""

    event_id: str
    score: float = 0.0


@dataclass
class MountedEvent:
    """挂载到上下文的完整文本。"""

    event_id: str
    content: str  # 已带时序标记的完整挂载文本


def format_mount(event_id: str, created_at: int, status: EventStatus, text: str) -> str:
    """把事件组装成带时序标记的挂载文本。

    格式：
    [回忆事件 | ID: {event_id} | 日期: {YYYY-MM-DD HH:MM} | 状态: {中文状态}]
    {text}
    [回忆结束]
    """

    label = _STATUS_LABEL.get(status, status.name)
    created_dt = datetime.fromtimestamp(created_at, tz=timezone.utc)
    date_str = created_dt.strftime("%Y-%m-%d %H:%M")
    return (
        f"[回忆事件 | ID: {event_id} | 日期: {date_str} | 状态: {label}]\n"
        f"{text}\n"
        "[回忆结束]"
    )
