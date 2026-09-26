"""SSDStorage：事件原文内容寻址 + 元数据落库。

- event_id = blake2b(原文 UTF-8, digest_size=8) 的 16 字符 hexdigest，事件不可变。
- 元数据写入 SQLite（WAL 模式），原文写入 {root}/events/shard_xx/{event_id}.txt，逐字节无损。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from event_memory.models import EventMeta, EventStatus


class SSDStorage:
    """内容寻址的本地事件存储。

    目录结构（懒创建）：
        {root}/events/shard_xx/   xx 为 event_id 前 2 个字符
        {root}/meta.db            SQLite，WAL 模式
    """

    def __init__(self, workspace_root: Path):
        self._root = Path(workspace_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._events_root = self._root / "events"
        self._db_path = self._root / "meta.db"

        # check_same_thread=False：允许 HTTP 多线程服务复用同一连接；
        # 调用方需自行串行化（sidecar 用全局锁），单线程测试不受影响。
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id   TEXT PRIMARY KEY,
                workspace  INTEGER NOT NULL DEFAULT 0,
                status     INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                hard_keys  TEXT NOT NULL,
                soft_keys  TEXT NOT NULL,
                summary    TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.commit()

    # ---- 写 ----

    def write_event(self, text: str, meta: EventMeta) -> str:
        """写入事件：原文内容寻址，元数据去重入库。

        重复原文（event_id 冲突）直接返回已有 id，不重复写文件。
        """
        event_id = hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()

        existing = self._conn.execute(
            "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing is not None:
            return event_id

        shard_dir = self._events_root / f"shard_{event_id[:2]}"
        shard_dir.mkdir(parents=True, exist_ok=True)
        (shard_dir / f"{event_id}.txt").write_bytes(text.encode("utf-8"))

        self._conn.execute(
            "INSERT OR IGNORE INTO events "
            "(event_id, workspace, status, created_at, hard_keys, soft_keys, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                int(meta.workspace),
                int(meta.status),
                int(meta.created_at),
                json.dumps(meta.hard_keys, ensure_ascii=False),
                json.dumps(meta.soft_keys, ensure_ascii=False),
                meta.summary or "",
            ),
        )
        self._conn.commit()
        return event_id

    # ---- 读 ----

    def read_event(self, event_id: str) -> str:
        """读原文，必须与写入逐字节一致；文件不存在抛 KeyError。"""
        path = self._events_root / f"shard_{event_id[:2]}" / f"{event_id}.txt"
        if not path.exists():
            raise KeyError(event_id)
        return path.read_bytes().decode("utf-8")

    def get_meta(self, event_id: str) -> EventMeta:
        """按 id 取元信息。"""
        row = self._conn.execute(
            "SELECT event_id, workspace, status, created_at, hard_keys, soft_keys, summary "
            "FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return self._row_to_meta(row)

    def list_all(self) -> list[EventMeta]:
        """返回全部事件元信息的列表（按 created_at、event_id 排序，稳定可测）。"""
        rows = self._conn.execute(
            "SELECT event_id, workspace, status, created_at, hard_keys, soft_keys, summary "
            "FROM events ORDER BY created_at, event_id"
        ).fetchall()
        return [self._row_to_meta(row) for row in rows]

    # ---- 辅助 ----

    @staticmethod
    def _row_to_meta(row: sqlite3.Row) -> EventMeta:
        return EventMeta(
            event_id=row["event_id"],
            created_at=row["created_at"],
            workspace=row["workspace"],
            status=EventStatus(row["status"]),
            hard_keys=json.loads(row["hard_keys"]),
            soft_keys=json.loads(row["soft_keys"]),
            summary=row["summary"] or "",
        )

    def close(self) -> None:
        self._conn.close()
