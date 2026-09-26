"""EventMemory：检索流程总装（llm=None 时跳过二次判断）。

流程：关键词提取 -> 倒排降级匹配(L1-L4) -> 候选 -> 二次判断 -> 挂载。

本阶段不接 LLM：`llm=None` 时跳过二次判断，直接信任 `index.lookup` 的匹配结果，
`query_hard`/`query_soft` 由调用方手动传入。

挂载：命中候选经 `load_and_mount` 读原文、按 `config.mount_max_tokens` 估算 token 并
截断（围绕 query 关键词取前后各约一半预算的字符窗口，Python `str` 天然保证 UTF-8 安全），
再用 `format_mount` 包装。卸载进热缓存，`remount` 先查热缓存再走文件。
关键词全 miss 时上层可用 `browse`（时间坐标系）浏览兜底。

仅使用标准库。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path

from event_memory.config import Config
from event_memory.extractor import Extractor
from event_memory.hotcache import HotCache
from event_memory.index import InvertedIndex, TimeIndex
from event_memory.models import EventMeta, EventStatus, MountedEvent, format_mount
from event_memory.storage import SSDStorage

# 压缩占位符：调用方用它替换已归档的历史段。
COMPRESS_PLACEHOLDER = "[事件已归档, 可用关键词检索]"

# 挂载截断时切分 query 关键词用：空白与常见中英文标点。
_QUERY_SPLIT_RE = re.compile(r"\s+|[，。！？、；：·…—《》〈〉「」『』【】（）“”‘’,.!?;:()\[\]{}<>]+")


def _now() -> int:
    """当前 unix 时间戳（秒）。"""
    return int(datetime.now(timezone.utc).timestamp())


def _is_cjk(ch: str) -> bool:
    """CJK 统一表意文字（用于 token 粗估）。"""
    return "\u4e00" <= ch <= "\u9fff"


class Session:
    """一个任务 = 一个会话。上下文由 agent 编排层管理，Session 只记 pending。"""

    def __init__(self, memory: "EventMemory", session_id: str, task: str) -> None:
        self.session_id = session_id
        self.pending: list[dict] = [{"role": "user", "content": task}]
        self._memory = memory

    def add_message(self, role: str, content: str) -> None:
        """记录每轮输入/输出到 pending（未落盘缓冲）。"""
        self.pending.append({"role": role, "content": content})

    def estimate_tokens(self) -> int:
        """粗估 token：中文字符*1 + 其他字符/4，对总和向上取整（只算 content）。"""
        total = 0.0
        for msg in self.pending:
            for ch in msg.get("content", ""):
                total += 1.0 if _is_cjk(ch) else 0.25
        return math.ceil(total)

    def end_session(self, seal: bool = True) -> None:
        """结束会话：seal=True 时 pending 无条件落盘（不等阈值），然后清空。"""
        if seal and self.pending:
            self._memory.compress(self.pending)
        self.pending = []


class EventMemory:
    """事件记忆总装：SSDStorage + InvertedIndex + TimeIndex + HotCache。

    启动时从 `storage.list_all()` rebuild 两个内存索引；`HotCache` 作为挂载/卸载
    之间的热层。
    """

    def __init__(self, workspace_root: Path, llm=None) -> None:
        self._workspace_root = Path(workspace_root)
        self._llm = llm
        self._config = Config(workspace_root=self._workspace_root)

        self._storage = SSDStorage(self._workspace_root)
        self._index = InvertedIndex()
        self._time = TimeIndex()
        self._hotcache = HotCache(max_entries=self._config.hot_cache_entries)

        # 索引重建：从落库事件全量恢复倒排与时间索引。
        self.rebuild()

        # browse 生成的序号 -> event_id 映射（本次会话内有效）。
        self._browse_map: dict[int, str] = {}

        # llm 有值时封装成 Extractor（分段/提词/判断统一入口）；None 则全程跳过 LLM。
        self._extractor = Extractor(llm) if llm is not None else None

    # ---- 索引重建 ----

    def rebuild(self) -> None:
        """从 storage.list_all() 全量重建两个内存索引。"""
        metas = self._storage.list_all()
        self._index.rebuild(metas)
        self._time.rebuild(metas)

    # ---- 写入 ----

    def add_event(
        self,
        text: str,
        hard_keys: list[str],
        soft_keys: list[str],
        summary: str = "",
        created_at: int | None = None,
    ) -> str:
        """手动写入事件（测试用，也是后续 seal 的底层）。

        去重靠 storage 主键：重复原文返回已存在的 event_id；若该 id 索引里已有，
        跳过重复插入。
        """
        if created_at is None:
            created_at = _now()

        meta = EventMeta(
            event_id="",
            created_at=int(created_at),
            workspace=0,
            status=EventStatus.SEALED,
            hard_keys=list(hard_keys or []),
            soft_keys=list(soft_keys or []),
            summary=summary,
        )
        event_id = self._storage.write_event(text, meta)

        # 去重：event_id 已存在且索引中已有，则跳过重复插入。
        if event_id not in self._index._created:
            self._index.insert(event_id, meta.hard_keys, meta.soft_keys)
            self._time.insert(event_id, meta.created_at)
            # 记录 created_at 供 lookup 的时间窗过滤与打分使用。
            self._index._created[event_id] = int(created_at)

        return event_id

    # ---- 检索 ----

    def _since_ts(self) -> int:
        """检索时间窗起点：now - time_window_days 天（0 表示不限）。"""
        window_days = self._config.time_window_days
        if window_days <= 0:
            return 0
        return max(0, _now() - int(window_days) * 86400)

    def retrieve(
        self,
        query: str,
        query_hard: list[str] | None = None,
        query_soft: list[str] | None = None,
    ) -> list[MountedEvent]:
        """检索并挂载命中事件。

        llm=None 时二次判断跳过，`query_hard`/`query_soft` 必须由调用方提供；
        有 LLM 且未提供时自动从 query 提词。
        """
        if query_hard is None:
            if self._extractor is not None:
                query_hard, query_soft, _ = self._extractor.extract(query)
            else:
                raise ValueError("llm=None 时 query_hard 必须由调用方提供")

        candidates = self._index.lookup(
            query_hard,
            query_soft or [],
            since_ts=self._since_ts(),
            time_window_days=self._config.time_window_days,
        )

        if self._llm is None:
            # 跳过二次判断，直接信任匹配结果。
            keep = {c.event_id for c in candidates}
        else:
            # 二次判断：宁可不命中、不要错命中。
            metas = {c.event_id: self._storage.get_meta(c.event_id) for c in candidates}
            relevant = set(self._extractor.judge(query, candidates, metas))
            keep = {c.event_id for c in candidates if c.event_id in relevant}

        mounts = []
        for event_id in keep:
            text = self._hotcache.get(event_id)
            if text is None:
                text = self._storage.read_event(event_id)
            mounts.append(self._mount_with_window(event_id, text, query or ""))
        return mounts

    # ---- 时间坐标系兜底 ----

    def browse(self, start_ts: int | None = None, end_ts: int | None = None, n: int = 20) -> str:
        """时间坐标系兜底浏览，返回多行 "[YYYY-MM-DD HH:MM] #{序号} {摘要}"。

        start/end 为 None 时用 recent(n)；生成 序号 -> event_id 映射（本次会话内有效）。
        摘要从 EventMeta.summary 取。
        """
        if start_ts is None and end_ts is None:
            event_ids = self._time.recent(n)
        else:
            lo = start_ts if start_ts is not None else 0
            hi = end_ts if end_ts is not None else (1 << 62)
            event_ids = self._time.range(lo, hi)

        self._browse_map = {}
        lines = []
        for i, event_id in enumerate(event_ids, start=1):
            meta = self._storage.get_meta(event_id)
            dt = datetime.fromtimestamp(
                meta.created_at, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M")
            summary = meta.summary or ""
            lines.append(f"[{dt}] #{i} {summary}")
            self._browse_map[i] = event_id

        return "\n".join(lines)

    def pick(self, event_no: int) -> MountedEvent:
        """browse 给的序号 -> 原文挂载（先查热缓存再走文件，原文不截断）。"""
        event_id = self._browse_map[event_no]
        text = self._read_text(event_id)
        meta = self._storage.get_meta(event_id)
        return MountedEvent(event_id, format_mount(event_id, meta.created_at, meta.status, text))

    # ---- 卸载 / 重新挂载 ----

    def unmount(self, event_id: str) -> None:
        """卸载进热缓存。"""
        self._hotcache.put(event_id, self._read_text(event_id))

    def remount(self, event_id: str) -> MountedEvent:
        """重新挂载：先热缓存，miss 走文件（原文不截断）。"""
        text = self._read_text(event_id)
        meta = self._storage.get_meta(event_id)
        return MountedEvent(
            event_id, format_mount(event_id, meta.created_at, meta.status, text)
        )

    def load_and_mount(self, event_id: str, query_text: str) -> MountedEvent:
        """读原文 -> 超预算则截断（围绕 query 关键词取窗口）-> format_mount 包装。"""
        text = self._read_text(event_id)
        return self._mount_with_window(event_id, text, query_text or "")

    # ---- 压缩（T1，接上下文阈值钩子）----

    def compress(self, history: list[dict]) -> str:
        """把对话历史压缩：LLM 切已完成事件 -> 提词+摘要 -> 落盘+索引。

        返回占位符文本，调用方用它替换原历史段。
        llm=None 时把整个 history 格式化为一个事件落盘（无关键词，仅可经 browse/pick 找回）。
        """
        if self._extractor is not None:
            texts = self._extractor.segment(history)
        elif history:
            # llm=None 降级：整体作为一个事件，逐字保留原文。
            texts = ["\n".join(
                f"{m.get('role', 'user')}: {m.get('content', '')}" for m in history
            )]
        else:
            texts = []

        for text in texts:
            if not text:
                continue
            if self._extractor is not None:
                hard, soft, summary = self._extractor.extract(text)
            else:
                hard, soft, summary = [], [], text[:50]
            self.add_event(text, hard, soft, summary=summary)

        return COMPRESS_PLACEHOLDER

    # ---- 完整检索（LLM 提词 + 二次判断）----

    def retrieve_full(
        self,
        query: str,
        llm_query_extract: bool = True,
        max_mount: int | None = None,
    ) -> list[MountedEvent]:
        """完整检索：提 query 关键词 -> 降级匹配 -> 二次判断 -> 挂载。

        llm=None 或 llm_query_extract=False 时把 query 本身当作唯一硬关键词。
        判断结果为空则返回 []（宁可不命中、不要错命中）。
        """
        if max_mount is None:
            max_mount = self._config.retrieval_candidates

        if self._extractor is not None and llm_query_extract:
            hard, soft, _ = self._extractor.extract(query)
        else:
            hard, soft = [query], []

        candidates = self._index.lookup(
            hard,
            soft,
            since_ts=self._since_ts(),
            time_window_days=self._config.time_window_days,
        )
        if not candidates:
            return []

        if self._extractor is not None:
            metas = {c.event_id: self._storage.get_meta(c.event_id) for c in candidates}
            relevant = set(self._extractor.judge(query, candidates, metas))
            candidates = [c for c in candidates if c.event_id in relevant]
            if not candidates:
                return []

        return [self.load_and_mount(c.event_id, query) for c in candidates[:max_mount]]

    # ---- 会话生命周期（任务 = 会话，agent 管理上下文）----

    def begin_session(self, task: str, session_id: str) -> Session:
        """agent 为任务开新会话：上下文干净起步，task 作为第一条 pending 消息。"""
        return Session(self, session_id, task)

    # ---- 辅助 ----

    def stats(self) -> dict:
        """事件总数 / 索引关键词数 / posting 数 / 热缓存条数。"""
        postings = sum(len(v) for v in self._index.postings.values())
        return {
            "events": len(self._storage.list_all()),
            "keywords": len(self._index.kw2id),
            "postings": postings,
            "hotcache_entries": len(self._hotcache),
        }

    # ---- 内部 ----

    def _read_text(self, event_id: str) -> str:
        """读原文：先热缓存，miss 走文件。"""
        text = self._hotcache.get(event_id)
        if text is None:
            text = self._storage.read_event(event_id)
        return text

    def _estimate_tokens(self, text: str) -> int:
        """估算 token 数（约 4 字符/token）。"""
        return math.ceil(len(text) / 4.0)

    def _mount_with_window(self, event_id: str, text: str, query_text: str) -> MountedEvent:
        """超预算则按 query 关键词截取字符窗口，再 format_mount 包装。

        Python `str` 切片天然按 Unicode 码点切，保证 UTF-8 不切坏。
        窗口定位：先找 query 原文，找不到则找 query 的各个关键词（按空白/
        标点切分），取最早出现位置，前后各取约一半预算；都不存在才退回首部。
        """
        budget = self._config.mount_max_tokens
        if self._estimate_tokens(text) <= budget:
            window = text
        else:
            budget_chars = budget * 4  # token 预算转字符预算
            pos = self._find_query_pos(text, query_text or "")
            if pos != -1:
                start = max(0, pos - budget_chars // 2)
                end = min(len(text), pos + budget_chars // 2)
            else:
                start = 0
                end = min(len(text), budget_chars)
            window = text[start:end]

        meta = self._storage.get_meta(event_id)
        return MountedEvent(
            event_id, format_mount(event_id, meta.created_at, meta.status, window)
        )

    @staticmethod
    def _find_query_pos(text: str, query_text: str) -> int:
        """query 关键词在事件原文中的首次出现位置；不存在返回 -1。

        候选依序：query 原文、按空白/标点切出的各关键词；返回最早的位置。
        """
        if not query_text:
            return -1
        candidates = [query_text]
        candidates.extend(
            tok for tok in _QUERY_SPLIT_RE.split(query_text) if len(tok) >= 2
        )
        best = -1
        for cand in candidates:
            pos = text.find(cand)
            if pos != -1 and (best == -1 or pos < best):
                best = pos
        return best
