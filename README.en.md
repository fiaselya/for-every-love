# pi-event-memory (for every love)

A standard **pi package** that adds **workspace event memory (event_memory)** and **workspace page management** to the [pi coding agent](https://github.com/badlogic/pi-mono).

[中文](README.md) | English

## Highlights

- **Memory is code-managed**: retrieval, mounting, truncation, dedup and archiving are all enforced in code — the model only presses tool buttons and never touches its own context
- **Lossless originals**: events are stored verbatim, in any language; hits mount the full original text, not a summary
- **Reliable dedup**: blake2b content addressing merges duplicate writes automatically; a misfire costs at most one extra event
- **Tiered fallback retrieval**: keyword miss falls back to a time-coordinate list — prefer a miss over a wrong hit
- **Auto-archiving**: original text is archived before context compaction, so folded turns are always recoverable
- **Pages hard-limited to workspace**: create/delete only inside `pages/`, path traversal rejected outright

## Package layout (the repo *is* the package)

```
package.json            pi manifest (pi-package keyword, pi.extensions → extensions/)
extensions/for-every-love.ts   the extension
agent/server.py         event_memory HTTP sidecar (127.0.0.1:8766, localhost only)
event_memory/           Python memory core (event segmentation + inverted index + lossless storage)
agent/workspace/        default workspace when used inside this repo (memory/ + pages/)
```

## Install

```bash
pi install git:github.com/fiaselya/for-every-love   # git
pi install "/path/to/for every love"                # local path
pi install npm:pi-event-memory                      # npm (optional)
```

Personal scope (`~/.pi/agent/settings.json`) makes it available in every project; `pi install -l` scopes it to the current project (requires project trust). `pi list` to inspect, `pi remove` to uninstall.

## Configuration

| Env var | Default | Description |
|---|---|---|
| `FEL_WORKSPACE` | `<cwd>/agent/workspace` | Per-project memory workspace (memory/ + pages/) |
| `FEL_PORT` | `8766` | Sidecar port (localhost only) |
| `FEL_PYTHON` | bundled `.venv/bin/python` → system `python3` | Sidecar interpreter |
| `FEL_LLM_CONFIG` | `<package root>/llm_config.json` | Housekeeping LLM (see `llm_config.example.json`; falls back to `llm=None` if missing or httpx is not installed) |

The sidecar is auto-started on demand (localhost only) and shut down when the session ends.

## Tools the agent gets

| Tool | Description |
|---|---|
| memory_search | Retrieve workspace memory and mount hits (falls back to a time-coordinate list on miss) |
| memory_browse / memory_pick | Browse by time / mount full original text by index |
| memory_save | Persist cross-session memory (content-addressed dedup) |
| page_create / page_read / page_delete / page_list | Workspace page management (hard-limited to `pages/`, `..` `/` `\` rejected) |
| /memory | Command: show memory stats |

Automatic behaviors (all executed by code, the model only triggers):
- Task start: relevant memories are auto-retrieved and injected into the system prompt.
- Before compaction: folded conversation turns are auto-archived into memory (original text always recoverable).
- Session end (quit/new): the last conversation segment is sealed to disk.
- Built-in write/edit are guarded to workspace-only.

## Requirements

- pi ≥ 0.87 (`@earendil-works/pi-coding-agent`)
- Python 3 for the sidecar; the memory core is stdlib-only (`httpx` optional)

## Design principle

**The orchestrator manages context, not the model.** Retrieval, mounting, truncation, dedup and archiving are all enforced in code; the model can only press tool buttons. Worst case of a misfire is one extra stored event (dedup absorbs it) — it can never corrupt the index or existing memory. "Prefer a miss over a wrong hit."

## License

MIT
