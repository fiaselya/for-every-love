"""SSDStorage 的测试：内容寻址去重 + 元数据持久化。"""

import hashlib

import pytest

from event_memory.models import EventMeta, EventStatus
from event_memory.storage import SSDStorage


def _event_id(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()


def _shard_dir(root):
    return root / "events"


def test_write_then_read_is_byte_for_byte(tmp_path):
    s = SSDStorage(tmp_path)
    text = "你好，世界！\n第二行\n\nemoji: 🚀🎉\n\ttab 保持"
    event_id = s.write_event(text, EventMeta(event_id="", created_at=1700000000))
    assert event_id == _event_id(text)
    assert s.read_event(event_id) == text


def test_same_text_twice_returns_same_id_and_single_file(tmp_path):
    s = SSDStorage(tmp_path)
    text = "不可变事件内容"
    id1 = s.write_event(text, EventMeta(event_id="", created_at=1))
    id2 = s.write_event(text, EventMeta(event_id="", created_at=2))
    assert id1 == id2
    shard_dir = _shard_dir(tmp_path) / f"shard_{id1[:2]}"
    files = list(shard_dir.glob("*.txt"))
    assert len(files) == 1
    assert files[0].name == f"{id1}.txt"


def test_get_meta_roundtrip(tmp_path):
    s = SSDStorage(tmp_path)
    meta = EventMeta(
        event_id="",
        created_at=1700000000,
        workspace=3,
        status=EventStatus.SEALED,
        hard_keys=["python", "sqlite"],
        soft_keys=["检索", "去重"],
        summary="一句话摘要",
    )
    event_id = s.write_event("原始文本内容", meta)
    got = s.get_meta(event_id)
    assert got.event_id == event_id
    assert got.created_at == 1700000000
    assert got.workspace == 3
    assert got.status == EventStatus.SEALED
    assert got.hard_keys == ["python", "sqlite"]
    assert got.soft_keys == ["检索", "去重"]
    assert got.summary == "一句话摘要"


def test_list_all_persists_across_instances(tmp_path):
    s = SSDStorage(tmp_path)
    written = {}
    for i in range(5):
        text = f"事件内容 {i} 中文 🌟"
        event_id = s.write_event(text, EventMeta(event_id="", created_at=1700000000 + i, workspace=i))
        written[event_id] = i
    del s

    s2 = SSDStorage(tmp_path)
    all_meta = s2.list_all()
    assert len(all_meta) == len(written)
    by_id = {m.event_id: m for m in all_meta}
    for event_id, workspace in written.items():
        assert event_id in by_id
        assert by_id[event_id].workspace == workspace


def test_read_missing_event_raises_keyerror(tmp_path):
    s = SSDStorage(tmp_path)
    missing = "a" * 16
    with pytest.raises(KeyError):
        s.read_event(missing)
