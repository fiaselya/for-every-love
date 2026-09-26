"""端到端测试：完整流程 add_event/compress -> retrieve -> browse/pick 兜底 ->
unmount -> 会话闭环。全部 llm=None + 手动关键词。

覆盖场景：
  1. 检索回挂：鹈鹕骑车/游戏两事件，按关键词挂回，原文逐字一致。
  2. 坐标系兜底：关键词全 miss -> browse 列表 -> pick 挂回。
  3. 去重与重建：同文本写两次 stats 不变；rebuild 后检索结果一致。
  4. 卸载与热缓存：unmount -> remount 内容一致。
  5. 会话闭环：begin_session -> add_message -> end_session(seal) 后，
     新会话（新 EventMemory 实例）能经 browse/pick 找回内容（持久化验证）。
"""

from __future__ import annotations

from event_memory.memory import EventMemory, COMPRESS_PLACEHOLDER

ELY_TEXT = "用户：帮我做一个鹈鹕骑车的动画\n助手：完成了，鹈鹕戴着墨镜骑车"
GAME_TEXT = "用户：帮我做一个贪吃蛇游戏\n助手：完成了，方向键控制移动"


def _seed(m: EventMemory) -> tuple[str, str]:
    id_ely = m.add_event(ELY_TEXT, ["鹈鹕", "骑车"], ["动画"], summary="鹈鹕骑车动画")
    id_game = m.add_event(GAME_TEXT, ["贪吃蛇", "游戏"], ["编程"], summary="贪吃蛇游戏")
    return id_ely, id_game


# ---------------------------------------------------------------------------
# 场景 1：检索回挂
# ---------------------------------------------------------------------------

def test_e2e_keyword_remount_exact_text(tmp_path):
    m = EventMemory(tmp_path, llm=None)
    id_ely, id_game = _seed(m)

    # 切走鹈鹕事件（模拟已被压缩归档），只剩游戏在上下文——这里只验证检索本身
    mounts = m.retrieve(query="鹈鹕骑车", query_hard=["鹈鹕"])
    assert len(mounts) == 1
    assert mounts[0].event_id == id_ely
    # 原文逐字一致（挂在时序标记之间）
    assert ELY_TEXT in mounts[0].content
    assert mounts[0].content.startswith("[回忆事件 |")
    assert mounts[0].content.endswith("[回忆结束]")

    # 换个关键词也能命中游戏事件
    mounts_game = m.retrieve(query="游戏", query_hard=["游戏"])
    assert [x.event_id for x in mounts_game] == [id_game]


# ---------------------------------------------------------------------------
# 场景 2：坐标系兜底（关键词全 miss -> browse -> pick）
# ---------------------------------------------------------------------------

def test_e2e_coordinate_fallback_browse_pick(tmp_path):
    m = EventMemory(tmp_path, llm=None)
    _seed(m)

    # 关键词全 miss
    assert m.retrieve(query="随便什么", query_hard=["完全不存在的词"]) == []

    # browse 返回非空列表，两行，格式 "[日期] #序号 摘要"
    listing = m.browse()
    lines = [ln for ln in listing.split("\n") if ln.strip()]
    assert len(lines) == 2
    for ln in lines:
        assert ln.startswith("[")
        assert "] #" in ln

    # pick 序号挂回对应事件原文
    mounted = m.pick(1)
    assert mounted.content.endswith("[回忆结束]")
    # 序号 1 是最新事件（游戏），序号 2 是鹈鹕
    assert GAME_TEXT in mounted.content
    assert ELY_TEXT in m.pick(2).content


# ---------------------------------------------------------------------------
# 场景 3：去重与重建
# ---------------------------------------------------------------------------

def test_e2e_dedup_and_rebuild(tmp_path):
    m = EventMemory(tmp_path, llm=None)
    text = "不可变的鹈鹕事件原文"
    eid1 = m.add_event(text, ["鹈鹕"], [], summary="s1")
    count1 = m.stats()["events"]
    eid2 = m.add_event(text, ["鹈鹕"], [], summary="s2")
    count2 = m.stats()["events"]
    assert eid1 == eid2
    assert count1 == count2 == 1

    # 全量重建索引后检索结果一致
    before = m.retrieve(query="鹈鹕", query_hard=["鹈鹕"])
    m.rebuild()
    after = m.retrieve(query="鹈鹕", query_hard=["鹈鹕"])
    assert [x.event_id for x in before] == [x.event_id for x in after] == [eid1]
    assert after[0].content == before[0].content


# ---------------------------------------------------------------------------
# 场景 4：卸载与热缓存
# ---------------------------------------------------------------------------

def test_e2e_unmount_remount_hotcache(tmp_path):
    m = EventMemory(tmp_path, llm=None)
    id_ely, _ = _seed(m)

    original = m.load_and_mount(id_ely, "鹈鹕")
    m.unmount(id_ely)
    assert m.stats()["hotcache_entries"] == 1

    # 热缓存命中：即使文件读取不可用也应成功
    from unittest import mock
    with mock.patch.object(m._storage, "read_event", side_effect=RuntimeError("no io")):
        again = m.remount(id_ely)
    assert again.event_id == id_ely
    assert ELY_TEXT in again.content
    assert again.content == original.content


# ---------------------------------------------------------------------------
# 场景 5：会话闭环（llm=None：compress 整体落盘，browse/pick 找回）
# ---------------------------------------------------------------------------

def test_e2e_session_lifecycle_persistence(tmp_path):
    m = EventMemory(tmp_path, llm=None)

    session = m.begin_session("完成鹈鹕骑车任务", "sess-e2e")
    assert session.pending[0] == {"role": "user", "content": "完成鹈鹕骑车任务"}
    session.add_message("user", "加上墨镜细节")
    session.add_message("assistant", "鹈鹕骑车动画已全部完成，包含墨镜细节")
    assert session.estimate_tokens() > 0

    # 模拟上下文阈值触发的压缩
    placeholder = m.compress(session.pending)
    assert placeholder == COMPRESS_PLACEHOLDER
    assert m.stats()["events"] == 1  # llm=None 整体作为一个事件

    # end_session seal：pending 已由 compress 落盘，关闭后清空
    session.end_session(seal=True)
    assert session.pending == []

    # 新会话 = 新 EventMemory 实例（同一工作区），能找回内容
    m2 = EventMemory(tmp_path, llm=None)
    assert m2.stats()["events"] == 1
    listing = m2.browse()
    assert "#1" in listing
    mounted = m2.pick(1)
    assert "鹈鹕骑车动画已全部完成，包含墨镜细节" in mounted.content
    assert "完成鹈鹕骑车任务" in mounted.content

    # seal=False 的会话：内容不落盘，新会话找不到
    s2 = m.begin_session("丢弃任务", "sess-drop")
    s2.add_message("user", "这段不应该被记住的内容")
    s2.end_session(seal=False)
    m3 = EventMemory(tmp_path, llm=None)
    assert m3.stats()["events"] == 1  # 仍只有会话闭环那一条
    listing3 = m3.browse()
    assert "不应该被记住" not in listing3
