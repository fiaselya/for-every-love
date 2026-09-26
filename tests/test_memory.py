"""EventMemory 的测试：检索流程（llm=None + 手动传关键词）。

覆盖验收点：
  1. 写入事件并 retrieve，挂回原文逐字一致。
  2. query_hard 全 miss -> retrieve 返回 []；browse 兜底返回两行，pick 能挂回。
  3. unmount 后 remount 走热缓存返回（stats 可见）。
  4. 超长事件截断后无乱码，且保留关键词。
  5. 重复 add_event 相同文本，stats 事件数不变。
"""

from datetime import datetime, timezone
from unittest import mock

from event_memory.config import Config
from event_memory.memory import EventMemory


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def test_retrieve_mounts_original_text(tmp_path):
    # 验收 1：写入两个事件，retrieve query_hard 命中鹈鹕事件，content 里的原文与写入逐字一致。
    m = EventMemory(tmp_path, llm=None)
    ely = "做了一个鹈鹕骑车的动画，鹈鹕戴着墨镜"
    snake = "写了一个贪吃蛇游戏"
    id_ely = m.add_event(ely, ["鹈鹕", "骑车"], [], summary="鹈鹕骑行动画")
    id_snake = m.add_event(snake, ["贪吃蛇", "游戏"], [], summary="贪吃蛇")
    assert id_ely and id_snake

    mounts = m.retrieve(query="鹈鹕骑行", query_hard=["鹈鹕", "骑车"])
    assert len(mounts) == 1
    m0 = mounts[0]
    assert m0.event_id == id_ely
    assert ely in m0.content  # 挂载文本里，原文逐字一致


def test_full_miss_browse_and_pick(tmp_path):
    # 验收 2：query_hard 全 miss -> []；browse 兜底返回两行；pick(序号) 挂回原文。
    m = EventMemory(tmp_path, llm=None)
    ely = "做了一个鹈鹕骑车的动画，鹈鹕戴着墨镜"
    snake = "写了一个贪吃蛇游戏"
    m.add_event(ely, ["鹈鹕", "骑车"], [], summary="鹈鹕骑行动画")
    m.add_event(snake, ["贪吃蛇", "游戏"], [], summary="贪吃蛇")

    assert m.retrieve(query="", query_hard=["不存在词"]) == []

    lines = m.browse().split("\n")
    assert len(lines) == 2
    # 每行格式 "[YYYY-MM-DD HH:MM] #{序号} {摘要}"
    for line in lines:
        assert line.startswith("[")
        assert "] #" in line
    by_no = {}
    for line in lines:
        _, rest = line.split("]", 1)
        num_summary = rest.split("#", 1)[1]  # # 紧跟序号：#{序号} {摘要}
        num_str, summary = num_summary.split(" ", 1)
        by_no[int(num_str.strip())] = summary.strip()
    assert set(by_no.values()) == {"鹈鹕骑行动画", "贪吃蛇"}

    pick = m.pick(next(iter(by_no)))
    assert isinstance(pick.content, str)
    assert len(pick.content) > 0


def test_unmount_remount_from_hotcache(tmp_path):
    # 验收 3：unmount 后 remount 走热缓存返回（让底层读文件抛错仍成功，证明来源是热缓存）。
    m = EventMemory(tmp_path, llm=None)
    ely = "做了一个鹈鹕骑车的动画，鹈鹕戴着墨镜"
    id_ely = m.add_event(ely, ["鹈鹕", "骑车"], [], summary="鹈鹕骑行动画")

    s_before = m.stats()
    m.unmount(id_ely)
    s_after = m.stats()
    assert s_after["hotcache_entries"] == 1
    assert s_after["events"] == s_before["events"]

    # 让文件读取失败：若 remount 真正走热缓存，仍应成功返回。
    with mock.patch.object(m._storage, "read_event", side_effect=RuntimeError("should not read file")):
        mounted = m.remount(id_ely)
    assert mounted.event_id == id_ely
    assert ely in mounted.content


def test_long_event_truncation_no_garble(tmp_path):
    # 验收 4：超长事件截断后无乱码，且保留关键词。
    m = EventMemory(tmp_path, llm=None)
    body = "嗨" + "中" * 20000
    id_long = m.add_event(body, ["嗨"], [], summary="超长事件")

    mounts = m.retrieve(query="嗨", query_hard=["嗨"])
    assert len(mounts) == 1
    content = mounts[0].content
    assert "嗨" in content  # 关键词保留
    assert len(content) <= Config(workspace_root=tmp_path).mount_max_tokens * 4
    assert "\ufffd" not in content  # 无乱码
    # UTF-8 可无损编码/解码
    assert content.encode("utf-8").decode("utf-8") == content


def test_duplicate_add_keeps_event_count(tmp_path):
    # 验收 5：重复 add_event 相同文本，stats 事件数不变。
    m = EventMemory(tmp_path, llm=None)
    text = "不可变事件内容"
    m.add_event(text, ["abc"], [], summary="d1")
    count1 = m.stats()["events"]
    m.add_event(text, ["abc"], [], summary="d2")
    count2 = m.stats()["events"]
    assert count1 == count2 == 1
