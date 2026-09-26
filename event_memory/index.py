"""InvertedIndex / TimeIndex：纯内存倒排索引 + 时间索引。

检索策略（降级链次序固定，宁可不命中、不要错命中）：
    L1  _and          硬词全部精确命中（AND）
    L2  _and_substring 硬词作为子串匹配关键词（AND）
    L3  _partial      硬词命中>=1 且 软词命中>=min_soft（默认2）
    L4  _or           任一硬词精确命中（OR）
前一档非空即采用，后续档位不再执行。

仅使用标准库（bisect / operator / string）。
"""

from __future__ import annotations

import bisect
import operator
import string

from event_memory.models import Candidate, EventMeta


# 归一化字符集：空白 + 标点（ASCII 与常见中文标点）。
_PUNCTUATION = (
    string.punctuation + "，。！？、；：、·…—《》〈〉「」『』【】（）“”‘’“”《》"
)
_WHITESPACE = string.whitespace + "　"
_STRIP_CHARS = _PUNCTUATION + _WHITESPACE

_CREATED_AT = operator.itemgetter(0)


def _normalize_keyword(kw: str) -> str:
    """关键词归一化：小写化 + 去首尾的空白与标点。"""
    if not kw:
        return ""
    return kw.strip(_STRIP_CHARS).lower()


class InvertedIndex:
    """倒排索引：关键词 -> 事件，并保留每关键词在该事件的归一化权重。"""

    def __init__(self) -> None:
        self.kw2id: dict[str, int] = {}
        self.postings: dict[int, dict[str, float]] = {}
        self._created: dict[str, int] = {}  # event_id -> created_at（lookup 时间与打分用）

    # ---- 写入 ----

    def insert(self, event_id, hard_keys, soft_keys) -> None:
        """将单个事件插入倒排索引；关键词入索引前统一归一化。"""
        keys = list(hard_keys or []) + list(soft_keys or [])
        norm = [k for k in (_normalize_keyword(k) for k in keys) if k]
        if not norm:
            return

        total = len(norm)
        counts: dict[str, int] = {}
        for k in norm:
            counts[k] = counts.get(k, 0) + 1

        for kw, occ in counts.items():
            kid = self.kw2id.get(kw)
            if kid is None:
                kid = len(self.kw2id)
                self.kw2id[kw] = kid
                self.postings[kid] = {}
            self.postings[kid][event_id] = occ / total

    # ---- 降级链 ----

    def _and(self, hard) -> set[str]:
        """L1：所有硬词都精确命中（交集）。"""
        if not hard:
            return set()
        result: set[str] | None = None
        for h in hard:
            kid = self.kw2id.get(h)
            evs = set(self.postings[kid]) if kid is not None else set()
            result = evs if result is None else (result & evs)
        return result or set()

    def _and_substring(self, hard) -> set[str]:
        """L2：每个硬词作为子串匹配关键词（交集）。"""
        if not hard:
            return set()
        result: set[str] | None = None
        for h in hard:
            evs: set[str] = set()
            for kw, kid in self.kw2id.items():
                if h in kw:
                    evs |= set(self.postings[kid])
            result = evs if result is None else (result & evs)
        return result or set()

    def _partial(self, hard, soft, min_soft=2) -> set[str]:
        """L3：硬词命中>=1 且 软词命中>=min_soft。"""
        pool: set[str] = set()
        for h in hard:
            kid = self.kw2id.get(h)
            if kid is not None:
                pool |= set(self.postings[kid])

        result: set[str] = set()
        for ev in pool:
            hard_hits = sum(
                1
                for h in hard
                if (kid := self.kw2id.get(h)) is not None and ev in self.postings[kid]
            )
            soft_hits = sum(
                1
                for s in soft
                if (kid := self.kw2id.get(s)) is not None and ev in self.postings[kid]
            )
            if hard_hits >= 1 and soft_hits >= min_soft:
                result.add(ev)
        return result

    def _or(self, hard) -> set[str]:
        """L4：任一硬词精确命中（并集）。"""
        result: set[str] = set()
        for h in hard:
            kid = self.kw2id.get(h)
            if kid is not None:
                result |= set(self.postings[kid])
        return result

    # ---- 查询 ----

    def lookup(self, hard, soft, since_ts, time_window_days=30) -> list[Candidate]:
        """严格按 L1->L2->L3->L4 降级；结果过滤 created_at >= since_ts；打分取 top5。"""
        now_ts = since_ts + int(time_window_days) * 86400

        hard = [k for k in (_normalize_keyword(k) for k in (hard or [])) if k]
        soft = [k for k in (_normalize_keyword(k) for k in (soft or [])) if k]

        candidates = self._and(hard)
        if not candidates:
            candidates = self._and_substring(hard)
        if not candidates:
            candidates = self._partial(hard, soft)
        if not candidates:
            candidates = self._or(hard)

        # 时间窗约束：任何档位都不放弃 created_at >= since_ts。
        candidates = {ev for ev in candidates if self._created.get(ev, 0) >= since_ts}
        if not candidates:
            return []

        scored: list[tuple[float, int, str]] = []
        for ev in candidates:
            created = self._created.get(ev, 0)
            d_days = (now_ts - created) / 86400.0
            if d_days < 0.0:
                d_days = 0.0
            soft_sum = 0.0
            for s in soft:
                kid = self.kw2id.get(s)
                if kid is not None:
                    soft_sum += self.postings[kid].get(ev, 0.0)
            score = soft_sum + 1.0 / (1.0 + d_days)
            scored.append((score, created, ev))

        # 排序：软词权重高 + 时间更近 的候选排前面；同分按创建时间更近、再按 id。
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [Candidate(event_id=ev, score=score) for score, created, ev in scored[:5]]

    # ---- 重建 ----

    def rebuild(self, metas: list[EventMeta]) -> None:
        """从元数据全量重建倒排索引与时间信息。"""
        self.kw2id = {}
        self.postings = {}
        self._created = {}
        for m in metas:
            self._created[m.event_id] = m.created_at
            self.insert(m.event_id, m.hard_keys, m.soft_keys)


class TimeIndex:
    """时间索引：按 (created_at, event_id) 升序保存，插入时保持有序。"""

    def __init__(self) -> None:
        self._sorted: list[tuple[int, str]] = []

    def insert(self, event_id, created_at) -> None:
        """用 bisect 保持按 (created_at, event_id) 有序。"""
        bisect.insort(self._sorted, (int(created_at), event_id))

    def range(self, start_ts, end_ts) -> list[str]:
        """返回 start_ts <= created_at <= end_ts 的事件（含头含尾）。"""
        lo = bisect.bisect_left(self._sorted, start_ts, key=_CREATED_AT)
        hi = bisect.bisect_right(self._sorted, end_ts, key=_CREATED_AT)
        return [eid for _, eid in self._sorted[lo:hi]]

    def recent(self, n) -> list[str]:
        """返回最近 n 条的 event_id（最新在前）。"""
        if n <= 0:
            return []
        tail = self._sorted[-n:][::-1]
        return [eid for _, eid in tail]

    def rebuild(self, metas) -> None:
        """从元数据全量重建，按 (created_at, event_id) 有序。"""
        self._sorted = []
        for m in metas:
            bisect.insort(self._sorted, (int(m.created_at), m.event_id))
