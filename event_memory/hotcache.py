"""HotCache：基于 OrderedDict 的 LRU 热缓存。

设计约束：
- 仅做性能优化，不改变语义。事件卸载（evict）后从缓存逐出，靠检索重新挂载。
- 仅存事件原文文本，键为 event_id（16 字符 blake2b hex）。

数据结构：
    OrderedDict 的插入顺序即 LRU 顺序：末尾为最新，开头为最旧。
    put/get 命中时把键移到末尾（move_to_end），最旧即最左端。
"""

from __future__ import annotations

from collections import OrderedDict


class HotCache:
    """LRU 热缓存：容量上限内保留最近访问的事件原文。

    - put：已存在则移到最新；超限逐出最旧（最左端）。
    - get：命中则移到最新；未命中返回 None。
    """

    def __init__(self, max_entries: int = 20) -> None:
        self._max_entries = max_entries
        self._store: OrderedDict[str, str] = OrderedDict()

    def put(self, event_id: str, text: str) -> None:
        """放入事件原文；已存在则移到最新；超限逐出最旧。"""
        if event_id in self._store:
            self._store.move_to_end(event_id, last=True)
        self._store[event_id] = text

        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)  # 逐出最旧（最左端）

    def get(self, event_id: str) -> str | None:
        """读取事件原文；命中则移到最新，未命中返回 None。"""
        if event_id not in self._store:
            return None
        self._store.move_to_end(event_id, last=True)
        return self._store[event_id]

    def __len__(self) -> int:
        return len(self._store)
