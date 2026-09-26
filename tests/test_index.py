"""InvertedIndex / TimeIndex 的测试：降级链检索 + 时间索引。

覆盖验收点：
  1. 两个硬词 AND：全命中命中；缺一个走不命中（L1 不返回）。
  2. L1 miss、"鹈鹕骑行" 含查询词 "鹈鹕" 子串场景 L2 命中。
  3. L2 miss、2 硬词(命中 1 个) + 2 软词场景 L3 命中。
  4. L3 miss、单硬词 OR 场景 L4 命中。
  5. 全部 miss 返回空列表。
  6. 时间窗过滤：since_ts 之前的候选任何档都不出现。
  7. TimeIndex.range 边界(含头含尾)、recent 数量正确。
  8. rebuild 后 lookup 结果与重建前完全一致。
  9. 排序：软词权重高 + 时间更近的候选排前面。
"""

from event_memory.index import InvertedIndex, TimeIndex
from event_memory.models import EventMeta


def _ids(cands):
    return [c.event_id for c in cands]


# ---------------------------------------------------------------------------
# 验收 1：两个硬词 AND（L1）。全命中命中；缺一个走不命中（L1 不返回）。
# ---------------------------------------------------------------------------
def test_l1_and_full_hit_vs_missing():
    idx = InvertedIndex()
    idx.rebuild([
        EventMeta(event_id="full", created_at=100, hard_keys=["apple", "banana"],
                  soft_keys=["red", "sweet"]),
        EventMeta(event_id="one", created_at=100, hard_keys=["apple"],
                  soft_keys=["red"]),
    ])

    # 全命中：L1 返回同时含两个硬词的事件。
    assert idx._and(["apple", "banana"]) == {"full"}
    assert _ids(idx.lookup(["apple", "banana"], ["red", "sweet"], since_ts=0)) == ["full"]

    # 缺一个：cherry 不在索引中，L1 直接不返回（走降级链）。
    assert idx._and(["banana", "cherry"]) == set()


# ---------------------------------------------------------------------------
# 验收 2：L1 miss，但"鹈鹕骑行"是"鹈鹕"的扩展子串场景 L2 命中。
# ---------------------------------------------------------------------------
def test_l2_substring_hit():
    idx = InvertedIndex()
    idx.rebuild([
        EventMeta(event_id="bird", created_at=100, hard_keys=["鹈鹕骑行"], soft_keys=[]),
    ])

    # 精确词 "鹈鹕" 不在索引 → L1 miss。
    assert idx._and(["鹈鹕"]) == set()
    # "鹈鹕" 是"鹈鹕骑行"的子串 → L2 hit。
    assert idx._and_substring(["鹈鹕"]) == {"bird"}
    assert _ids(idx.lookup(["鹈鹕"], [], since_ts=0)) == ["bird"]


# ---------------------------------------------------------------------------
# 验收 3：L2 miss，2 硬词(命中 1 个) + 2 软词场景 L3 命中。
# ---------------------------------------------------------------------------
def test_l3_partial_hit():
    idx = InvertedIndex()
    idx.rebuild([
        EventMeta(event_id="E", created_at=100, hard_keys=["apple", "banana"],
                  soft_keys=["red", "sweet"]),
    ])

    # cherry 缺失 → L1/L2 都不返回。
    assert idx._and(["apple", "cherry"]) == set()
    assert idx._and_substring(["apple", "cherry"]) == set()
    # 硬词命中 1 个(apple) + 软词命中 2 个(red,sweet) → L3 hit。
    assert idx._partial(["apple", "cherry"], ["red", "sweet"]) == {"E"}
    assert _ids(idx.lookup(["apple", "cherry"], ["red", "sweet"], since_ts=0)) == ["E"]


# ---------------------------------------------------------------------------
# 验收 4：L3 miss，单硬词 OR 场景 L4 命中。
# ---------------------------------------------------------------------------
def test_l4_or_hit():
    idx = InvertedIndex()
    idx.rebuild([
        # 仅 1 个软词 → L3 的"软词命中>=2"不满足。
        EventMeta(event_id="E", created_at=100, hard_keys=["apple"], soft_keys=["red"]),
    ])

    assert idx._and(["apple", "banana"]) == set()
    assert idx._and_substring(["apple", "banana"]) == set()
    assert idx._partial(["apple", "banana"], ["red", "sweet"]) == set()
    assert idx._or(["apple", "banana"]) == {"E"}
    assert _ids(idx.lookup(["apple", "banana"], ["red", "sweet"], since_ts=0)) == ["E"]


# ---------------------------------------------------------------------------
# 验收 5：全部 miss 返回空列表。
# ---------------------------------------------------------------------------
def test_all_miss_returns_empty():
    idx = InvertedIndex()
    idx.rebuild([
        EventMeta(event_id="E", created_at=100, hard_keys=["apple"], soft_keys=["red"]),
    ])
    result = idx.lookup(["xyz", "uvw"], ["q", "w"], since_ts=0)
    assert result == []


# ---------------------------------------------------------------------------
# 验收 6：时间窗过滤，since_ts 之前的候选任何档都不出现。
# ---------------------------------------------------------------------------
def test_time_window_filters_all_levels():
    idx = InvertedIndex()
    idx.rebuild([
        EventMeta(event_id="old", created_at=100, hard_keys=["event"], soft_keys=["hot"]),
        EventMeta(event_id="new", created_at=200, hard_keys=["event"], soft_keys=["hot"]),
    ])

    result = idx.lookup(["event"], ["hot"], since_ts=150)
    ids = _ids(result)
    assert "new" in ids
    assert "old" not in ids  # 100 < 150，任何档都被过滤

    # 放宽到 0，old 重新出现（证明是时间窗在过滤）。
    result2 = idx.lookup(["event"], ["hot"], since_ts=0)
    assert _ids(result2) == ["new", "old"] or _ids(result2) == ["old", "new"]


# ---------------------------------------------------------------------------
# 验收 9：排序——软词权重高 + 时间更近的候选排前面。
# ---------------------------------------------------------------------------
def test_sort_recent_then_soft_weight():
    # (a) 时间更近排前面：软词权重相同，创建更近的分数更高。
    idx = InvertedIndex()
    idx.rebuild([
        EventMeta(event_id="A", created_at=100, hard_keys=["dog"], soft_keys=["puppy"]),
        EventMeta(event_id="B", created_at=200, hard_keys=["dog"], soft_keys=["puppy"]),
    ])
    r = idx.lookup(["dog"], ["puppy"], since_ts=0, time_window_days=30)
    assert _ids(r) == ["B", "A"]  # B 更新 → 时间衰减项更大 → 排前

    # (b) 软词权重高排前面：创建时间相同，软词权重更高的排前。
    idx2 = InvertedIndex()
    idx2.rebuild([
        # total=3 → puppy 权重 1/3
        EventMeta(event_id="P", created_at=200, hard_keys=["dog", "cat"], soft_keys=["puppy"]),
        # total=2 → puppy 权重 1/2
        EventMeta(event_id="Q", created_at=200, hard_keys=["dog"], soft_keys=["puppy"]),
    ])
    r2 = idx2.lookup(["dog"], ["puppy"], since_ts=0, time_window_days=30)
    assert _ids(r2) == ["Q", "P"]  # Q 的软词权重更高 → 排前


# ---------------------------------------------------------------------------
# 验收 8：rebuild 后 lookup 结果与重建前完全一致。
# ---------------------------------------------------------------------------
def test_rebuild_consistent_lookup():
    metas = [
        EventMeta(event_id="sw", created_at=100, hard_keys=["dog"], soft_keys=["puppy"]),
        EventMeta(event_id="sr", created_at=200, hard_keys=["dog"], soft_keys=["puppy"]),
        EventMeta(event_id="tag", created_at=300, hard_keys=["cat"], soft_keys=["meow"]),
    ]
    idx = InvertedIndex()
    idx.rebuild(metas)
    before = [(c.event_id, round(c.score, 6)) for c in idx.lookup(["dog", "cat"], ["puppy", "meow"], since_ts=0, time_window_days=30)]

    idx.rebuild(metas)  # 全量重建
    after = [(c.event_id, round(c.score, 6)) for c in idx.lookup(["dog", "cat"], ["puppy", "meow"], since_ts=0, time_window_days=30)]

    assert before == after


# ---------------------------------------------------------------------------
# 验收 7：TimeIndex.range 边界(含头含尾)、recent 数量正确。
# ---------------------------------------------------------------------------
def test_time_index_range_and_recent():
    tix = TimeIndex()
    for eid, ts in [("e1", 100), ("e3", 300), ("e5", 500), ("e2", 200), ("e4", 400)]:
        tix.insert(eid, ts)

    # 含头含尾：200..400 包含 e2,e3,e4。
    assert tix.range(200, 400) == ["e2", "e3", "e4"]

    # 单点区间也含头含尾。
    assert tix.range(300, 300) == ["e3"]

    # recent：最近 n 条，数量正确，且最新在前。
    assert _recent_ids(tix, 2) == ["e5", "e4"]
    assert _recent_ids(tix, 3) == ["e5", "e4", "e3"]
    assert _recent_ids(tix, 1) == ["e5"]
    assert _recent_ids(tix, 0) == []
    assert _recent_ids(tix, 99) == ["e5", "e4", "e3", "e2", "e1"]


def _recent_ids(tix, n):
    return tix.recent(n)
