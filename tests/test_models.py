from datetime import datetime, timezone

from event_memory.models import (
    Candidate,
    EventMeta,
    EventStatus,
    MountedEvent,
    format_mount,
)

EVENT_ID = "abc123def456"
CREATED_AT = 1700000000  # 2023-11-14 22:13 UTC


def test_event_status_values():
    assert EventStatus.ACTIVE == 0
    assert EventStatus.SEALED == 1
    assert EventStatus.ARCHIVED == 2


def test_event_meta_defaults():
    meta = EventMeta(event_id=EVENT_ID, created_at=CREATED_AT)
    assert meta.workspace == 0
    assert meta.status == EventStatus.SEALED
    assert meta.hard_keys == []
    assert meta.soft_keys == []
    assert meta.summary == ""


def test_candidate_defaults():
    cand = Candidate(event_id=EVENT_ID)
    assert cand.score == 0.0


def test_mounted_event_basic():
    mounted = MountedEvent(event_id=EVENT_ID, content="body text")
    assert mounted.event_id == EVENT_ID
    assert mounted.content == "body text"


def test_format_mount_sealed():
    result = format_mount(EVENT_ID, CREATED_AT, EventStatus.SEALED, "body text")
    expected = (
        "[回忆事件 | ID: abc123def456 | 日期: 2023-11-14 22:13 | 状态: 已完成]\n"
        "body text\n"
        "[回忆结束]"
    )
    assert result == expected


def test_format_mount_active():
    result = format_mount(EVENT_ID, CREATED_AT, EventStatus.ACTIVE, "body text")
    assert "状态: 进行中]" in result


def test_format_mount_archived():
    result = format_mount(EVENT_ID, CREATED_AT, EventStatus.ARCHIVED, "body text")
    assert "状态: 冷归档]" in result


def test_format_mount_empty_summary():
    result = format_mount(EVENT_ID, CREATED_AT, EventStatus.SEALED, "")
    assert result == (
        "[回忆事件 | ID: abc123def456 | 日期: 2023-11-14 22:13 | 状态: 已完成]\n"
        "\n"
        "[回忆结束]"
    )
