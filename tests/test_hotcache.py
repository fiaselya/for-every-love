"""HotCache 的测试：基于 OrderedDict 的 LRU 热缓存。

覆盖验收点：
  1. 容量逐出：塞 max_entries+1 条，最早（最旧）的逐出。
  2. get 命中会把该条变成最新，影响下一次逐出对象。
  3. get 未命中返回 None。
"""

from event_memory.hotcache import HotCache


def test_capacity_eviction_removes_oldest(tmp_path):
    # 验收 1：塞 max_entries+1 条，最早的最旧被逐出。
    cache = HotCache(max_entries=2)
    cache.put("a", "文本A")
    cache.put("b", "文本B")
    cache.put("c", "文本C")

    assert len(cache) == 2
    assert cache.get("a") is None          # 最旧的 a 被逐出
    assert cache.get("b") == "文本B"        # 保留
    assert cache.get("c") == "文本C"        # 保留


def test_get_hot_item_affects_next_eviction(tmp_path):
    # 验收 2：get 命中把该条变成最新，影响下一次逐出对象。
    cache = HotCache(max_entries=2)
    cache.put("a", "文本A")
    cache.put("b", "文本B")

    # 访问 a，使其成为最新；此时再插入 c，应逐出 b（最旧）。
    assert cache.get("a") == "文本A"
    cache.put("c", "文本C")

    assert len(cache) == 2
    assert cache.get("a") == "文本A"        # 热访问过，未被逐出
    assert cache.get("b") is None           # 被逐出
    assert cache.get("c") == "文本C"


def test_get_missing_returns_none(tmp_path):
    # 验收 3：get 未命中返回 None。
    cache = HotCache(max_entries=3)
    cache.put("a", "文本A")
    assert cache.get("zzz_missing") is None
    assert cache.get("") is None


def test_put_existing_moves_to_newest(tmp_path):
    # 已存在则移到最新（同样影响逐出对象）。
    cache = HotCache(max_entries=2)
    cache.put("a", "文本A")
    cache.put("b", "文本B")

    cache.put("a", "文本A-更新")  # 重新 put 已存在的 a，应移到最新
    cache.put("c", "文本C")        # 逐出最旧 b

    assert len(cache) == 2
    assert cache.get("a") == "文本A-更新"
    assert cache.get("b") is None
    assert cache.get("c") == "文本C"
