#!/usr/bin/env python3
"""event_memory HTTP sidecar —— 把事件记忆库暴露成 JSON API 给 pi agent 扩展调用。

- 工作区根固定为 agent/workspace/（memory/ 为记忆库，pages/ 由 pi 扩展管理）。
- 仅监听 127.0.0.1，不对外网开放。
- 全局锁串行化 EventMemory 操作（sqlite 连接 + 内存索引非线程安全）。
- 仅用标准库 http.server；依赖 event_memory 包（httpx 仅在 llm_config.json 存在时用）。

用法：.venv/bin/python agent/server.py  [默认端口 8766，可用 FEL_PORT 覆盖]
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = Path(os.environ.get("FEL_WORKSPACE") or (Path(__file__).resolve().parent / "workspace"))
MEMORY_ROOT = WORKSPACE / "memory"

sys.path.insert(0, str(PROJECT_ROOT))

from event_memory.llm import load_llm_client  # noqa: E402
from event_memory.memory import EventMemory  # noqa: E402

_LOCK = threading.Lock()
_LLM = load_llm_client(PROJECT_ROOT / "llm_config.json")
memory = EventMemory(MEMORY_ROOT, llm=_LLM)


# ---- API 处理函数：入参 dict，出参 (status_code, dict) ----

def api_health(_: dict):
    return 200, {"ok": True, "workspace": str(WORKSPACE), "llm": _LLM is not None}


def api_stats(_: dict):
    return 200, memory.stats()


def api_retrieve(body: dict):
    query = str(body.get("query", ""))
    query_hard = body.get("query_hard")
    query_soft = body.get("query_soft")
    if query_hard is None and _LLM is None:
        return 400, {"error": "llm=None 时必须提供 query_hard"}
    mounts = memory.retrieve(query=query, query_hard=query_hard, query_soft=query_soft)
    return 200, {"mounted": [{"event_id": m.event_id, "content": m.content} for m in mounts]}


def api_retrieve_full(body: dict):
    query = str(body.get("query", ""))
    if not query.strip():
        return 400, {"error": "query 不能为空"}
    mounts = memory.retrieve_full(query)
    return 200, {"mounted": [{"event_id": m.event_id, "content": m.content} for m in mounts]}


def api_browse(body: dict):
    listing = memory.browse(
        start_ts=body.get("start_ts"),
        end_ts=body.get("end_ts"),
        n=int(body.get("n", 20)),
    )
    items = []
    for no, event_id in memory._browse_map.items():  # noqa: SLF001 同进程内复用 browse 建立的映射
        meta = memory._storage.get_meta(event_id)  # noqa: SLF001
        items.append({"no": no, "event_id": event_id, "summary": meta.summary})
    return 200, {"listing": listing, "items": items}


def api_pick(body: dict):
    event_no = int(body.get("event_no", 0))
    if event_no <= 0:
        return 400, {"error": "event_no 必须是 browse 返回的正整数序号"}
    mounted = memory.pick(event_no)
    return 200, {"event_id": mounted.event_id, "content": mounted.content}


def api_add_event(body: dict):
    text = body.get("text")
    if not text or not str(text).strip():
        return 400, {"error": "text 不能为空"}
    event_id = memory.add_event(
        str(text),
        list(body.get("hard_keys") or []),
        list(body.get("soft_keys") or []),
        summary=str(body.get("summary") or ""),
        created_at=body.get("created_at"),
    )
    return 200, {"event_id": event_id}


def api_compress(body: dict):
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return 400, {"error": "messages 必须是 [{role, content}] 非空数组"}
    placeholder = memory.compress([dict(m) for m in messages])
    return 200, {"placeholder": placeholder}


def api_unmount(body: dict):
    event_id = str(body.get("event_id", ""))
    if not event_id:
        return 400, {"error": "event_id 不能为空"}
    memory.unmount(event_id)
    return 200, {"ok": True}


# ---- HTTP 层 ----

POST_ROUTES = {
    "/retrieve": api_retrieve,
    "/retrieve_full": api_retrieve_full,
    "/browse": api_browse,
    "/pick": api_pick,
    "/add_event": api_add_event,
    "/compress": api_compress,
    "/unmount": api_unmount,
}

GET_ROUTES = {
    "/health": api_health,
    "/stats": api_stats,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        # 只允许本机访问。
        if self.client_address[0] not in ("127.0.0.1", "localhost"):
            self._send(403, {"error": "forbidden"})
            return

        path = self.path.split("?", 1)[0]
        if method == "POST":
            handler = POST_ROUTES.get(path)
        else:
            handler = GET_ROUTES.get(path)
        if handler is None:
            self._send(404, {"error": f"unknown endpoint: {method} {path}"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                body = {}
            with _LOCK:
                status, payload = handler(body)
            self._send(status, payload)
        except KeyError as exc:
            self._send(404, {"error": f"not found: {exc}"})
        except (ValueError, TypeError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 兜底：记忆系统故障不拖死调用方
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def log_message(self, fmt, *args):  # 安静模式
        pass


def main() -> None:
    port = int(os.environ.get("FEL_PORT", "8766"))
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    WORKSPACE.joinpath("pages").mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[event_memory sidecar] workspace={WORKSPACE} llm={'on' if _LLM else 'off'} "
          f"listening on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
