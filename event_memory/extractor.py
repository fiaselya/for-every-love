"""Extractor：事件分段 / 关键词摘要 / 相关性判断，统一走 LLM。

原则：宁可不命中、不要错命中。LLM 解析失败或重试后仍失败，一律降级
返回保守结果，绝不抛异常、绝不误判。

llm=None 时整段跳过；关键词抽取在 jieba 不可用时退回标准库兜底。
"""

from __future__ import annotations

import json
import re
import string
from datetime import datetime, timezone

from event_memory.llm import LLMClient
from event_memory.prompts import EXTRACT_PROMPT, JUDGE_PROMPT, SEGMENT_PROMPT

# 关键词归一化：去首尾空白与标点 + 小写化。
_STRIP_CHARS = string.punctuation + string.whitespace + "，。！？、；：·…—《》〈〉「」『』【】（）“’　"
_HOT_KEYS_MAX = 5
_SOFT_KEYS_MAX = 8

# 标准库兜底：按空白或标点切词。
_SPLIT_RE = re.compile(r"\s+|[，。！？、；：·…—《》〈〉「」『』【】（）“”‘’,.!?;:()\[\]{}<>…]+")


def _normalize_keyword(kw: str) -> str:
    """关键词归一化：去首尾标点/空白并小写化。"""
    if not kw:
        return ""
    return kw.strip(_STRIP_CHARS).lower()


class Extractor:
    """封装 LLM 驱动的分段 / 关键词 / 判断三项能力。"""

    def __init__(self, llm: LLMClient | None) -> None:
        self._llm = llm

    # ---- 关键词与摘要 ----

    def extract(self, text: str) -> tuple[list[str], list[str], str]:
        """从 event 原文抽取 (hard, soft, summary)。

        llm=None：jieba 抽 soft top10，hard 为空，summary 截前 50 字。
        llm 有：走 EXTRACT_PROMPT；解析/重试仍失败则返回 ([], [], text[:50])。
        关键词后处理：小写化、去首尾标点，去重保序、按数量上限截断。
        """
        if self._llm is None:
            soft = self._fallback_soft(text)
            return [], soft, text[:50]

        # 解析失败重试 1 次，再失败直接降级（宁不命中）。
        for _attempt in range(2):
            try:
                result = self._llm.chat_json(
                    system="你是事件关键词与摘要提取助手，只输出 JSON。",
                    user=EXTRACT_PROMPT.replace("__EVENT__", text),
                )
                hard = result.get("hard") or []
                soft = result.get("soft") or []
                summary = result.get("summary") or ""
                return (
                    self._clean_keywords(hard, _HOT_KEYS_MAX),
                    self._clean_keywords(soft, _SOFT_KEYS_MAX),
                    str(summary)[:50],
                )
            except Exception:  # 解析/转写/重试全部失败 -> 降级
                continue

        return [], [], text[:50]

    def _fallback_soft(self, text: str) -> list[str]:
        """llm=None 时：优先 jieba.analyse.extract_tags(top10)，不可用则退回
        标准库按空白/标点切词去重取前 10。"""
        try:
            from jieba.analyse import extract_tags

            tags = [t for t in extract_tags(text, topK=10) if t]
            if tags:
                return tags
        except Exception:  # jieba 未安装或出错
            pass

        tokens = [t.strip(_STRIP_CHARS) for t in _SPLIT_RE.split(text) if t.strip(_STRIP_CHARS)]
        seen: set[str] = set()
        out: list[str] = []
        for t in tokens:
            if t not in seen:
                seen.add(t)
                out.append(t)
            if len(out) >= 10:
                break
        return out

    @staticmethod
    def _clean_keywords(raw, limit: int) -> list[str]:
        """关键词清洗：去空白/空串、去首尾标点、小写化，去重保序、截断。"""
        seen: set[str] = set()
        out: list[str] = []
        for item in raw or []:
            if not isinstance(item, str):
                continue
            kw = _normalize_keyword(item)
            if kw and kw not in seen:
                seen.add(kw)
                out.append(kw)
            if len(out) >= limit:
                break
        return out

    # ---- 分段 ----

    def segment(self, history: list[dict]) -> list[str]:
        """对话历史 -> 已完成事件原文列表。llm=None 或解析失败均返回 []。"""
        if self._llm is None:
            return []

        prompt = SEGMENT_PROMPT.replace("__HISTORY__", _format_history(history))

        for _attempt in range(2):
            try:
                result = self._llm.chat_json(
                    system="你是对话分段助手，只输出 JSON。",
                    user=prompt,
                )
                events = result.get("events") or []
                texts: list[str] = []
                for ev in events:
                    if isinstance(ev, dict) and isinstance(ev.get("text"), str):
                        t = ev["text"].strip()
                        if t:
                            texts.append(t)
                return texts
            except Exception:
                continue

        return []

    # ---- 相关性判断 ----

    def judge(
        self,
        query: str,
        candidates: list,
        metas: dict[str, object],
    ) -> list[str]:
        """(query, 候选) -> 去重后的相关 event_id 列表。失败视为都不相关，返回 []。

        candidates 每项含 event_id（dataclass 属性或 dict 键）；
        metas 为 event_id -> 元信息（含 summary/created_at）的映射。
        """
        if self._llm is None:
            return []

        prompt = JUDGE_PROMPT.replace("__QUERY__", query).replace(
            "__CANDIDATES__", self._build_candidates(candidates, metas)
        )

        for _attempt in range(2):
            try:
                result = self._llm.chat_json(
                    system="事件相关性判断助手，只输出 JSON。",
                    user=prompt,
                )
                relevant = result.get("relevant") or []
                seen_eids: set[str] = set()
                ids: list[str] = []
                for eid in relevant:
                    if isinstance(eid, str) and eid and eid not in seen_eids:
                        seen_eids.add(eid)
                        ids.append(eid)
                return ids
            except Exception:
                continue

        return []

    @staticmethod
    def _build_candidates(candidates, metas) -> str:
        """把候选 + 元信息拼成 JSON 数组文本，供 prompt 展示。"""
        items = []
        for cand in candidates or []:
            event_id = getattr(cand, "event_id", None)
            if event_id is None and isinstance(cand, dict):
                event_id = cand.get("event_id")
            meta = metas.get(event_id) if event_id is not None else None
            summary = ""
            date = ""
            if meta is not None:
                summary = getattr(meta, "summary", "") or ""
                created_at = getattr(meta, "created_at", 0) or 0
                dt = datetime.fromtimestamp(created_at, tz=timezone.utc)
                date = dt.strftime("%Y-%m-%d %H:%M")
            items.append({"event_id": event_id, "summary": summary, "date": date})
        return json.dumps(items, ensure_ascii=False)


# ---- 模块级辅助 ----

def _split_by_punct(text: str) -> list[str]:
    """按空白或标点切分中文/混合文本为候选词（标准库兜底用，供历史残留兼容）。"""
    return [p for p in _SPLIT_RE.split(text) if p]


def _format_history(history: list[dict]) -> str:
    """把对话历史（角色+内容列表）格式化为 '角色: 内容' 多行文本。"""
    lines = []
    for turn in history or []:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role") or turn.get("角色") or "user"
        content = turn.get("content") or turn.get("内容") or ""
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
