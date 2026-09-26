/**
 * pi-event-memory —— 工作区记忆 + 工作区页面管理扩展
 *
 * 能力：
 * 1. 工作区共享记忆（event_memory sidecar，默认 127.0.0.1:8766）：
 *    - 任务开始自动按任务挂载相关历史记忆（before_agent_start 注入 system prompt）
 *    - compaction 前自动归档被折叠的对话原文（session_before_compact）
 *    - 会话结束（quit/new）seal 最后一段对话（session_shutdown）
 *    - memory_search / memory_browse / memory_pick / memory_save 工具
 * 2. 工作区页面（仅限 <workspace>/pages/ 内，硬性防路径穿越）：
 *    - page_create / page_read / page_delete / page_list
 * 3. 越界拦截：内置 write/edit 工具只允许写工作区内路径。
 *
 * 路径约定（全部可用环境变量覆盖）：
 *   FEL_WORKSPACE  工作区根（默认 <cwd>/agent/workspace，含 memory/ 与 pages/）
 *   FEL_PORT       sidecar 端口（默认 8766）
 *   FEL_PYTHON     启动 sidecar 用的 python（默认：包内 .venv → 系统 python3）
 *   FEL_LLM_CONFIG sidecar 的 llm_config.json 路径（默认：包根/llm_config.json，缺省则 llm=None 降级）
 *
 * sidecar 未启动时会在首次使用时自动拉起，会话结束自动关闭。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import * as fs from "node:fs";
import * as path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

// ---- 路径解析（包相对，不硬编码） ----

function currentFile(): string {
  try {
    if (typeof import.meta !== "undefined" && import.meta.url) return fileURLToPath(import.meta.url);
  } catch {
    /* fallthrough */
  }
  try {
    if (typeof __filename !== "undefined") return __filename;
  } catch {
    /* fallthrough */
  }
  return ".";
}

const EXT_DIR = path.dirname(currentFile());
const PKG_ROOT = path.resolve(EXT_DIR, ".."); // extensions/ 的上一级 = 包根

const WORKSPACE = process.env.FEL_WORKSPACE || path.join(process.cwd(), "agent", "workspace");
const PAGES_DIR = path.join(WORKSPACE, "pages");
const SIDECAR = process.env.FEL_SIDECAR || "http://127.0.0.1:8766";
const SIDECAR_PORT = process.env.FEL_PORT || "8766";
const SERVER_SCRIPT = path.join(PKG_ROOT, "agent", "server.py");

function resolvePython(): string {
  if (process.env.FEL_PYTHON) return process.env.FEL_PYTHON;
  const venv = path.join(PKG_ROOT, ".venv", "bin", "python");
  if (fs.existsSync(venv)) return venv;
  return "python3";
}

// ---- sidecar HTTP 客户端 ----

let sidecarProc: ReturnType<typeof spawn> | null = null;

async function api(endpoint: string, body?: unknown): Promise<any> {
  const res = await fetch(`${SIDECAR}${endpoint}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(60_000),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(`sidecar ${endpoint} 失败: HTTP ${res.status} ${JSON.stringify(data)}`);
  }
  return data;
}

async function ensureSidecar(): Promise<void> {
  try {
    await api("/health");
    return;
  } catch {
    /* 未启动，尝试拉起 */
  }
  if (sidecarProc) {
    await sleep(800);
    return;
  }
  try {
    sidecarProc = spawn(resolvePython(), [SERVER_SCRIPT], {
      cwd: PKG_ROOT,
      env: {
        ...process.env,
        FEL_PORT: SIDECAR_PORT,
        FEL_WORKSPACE: WORKSPACE,
      },
      stdio: "ignore",
      detached: true,
    });
    sidecarProc.unref();
    await sleep(1500);
  } catch {
    /* 拉起失败：工具调用时会给出明确报错 */
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** 关闭由本扩展拉起的 sidecar（外部手动启动的不归我们管）。 */
function stopSidecar(): void {
  if (sidecarProc && sidecarProc.pid) {
    try {
      process.kill(-sidecarProc.pid); // detached 有独立进程组
    } catch {
      try {
        sidecarProc.kill();
      } catch {
        /* 已退出 */
      }
    }
    sidecarProc = null;
  }
}

// ---- 页面路径安全（硬性限制在工作区 pages/ 内） ----

function pagePath(name: string): string {
  const clean = String(name || "").trim().replace(/\.md$/i, "").trim();
  if (!clean) throw new Error("页面名不能为空");
  if (/[/\\]/.test(clean) || clean.includes("..") || clean.startsWith(".")) {
    throw new Error(`非法页面名 "${name}"：不允许路径分隔符 / \\ .. 或以 . 开头（页面仅限工作区 pages/ 内）`);
  }
  const pagesAbs = path.resolve(PAGES_DIR);
  const target = path.resolve(pagesAbs, `${clean}.md`);
  if (!target.startsWith(pagesAbs + path.sep)) {
    throw new Error("路径越界：页面只能创建在工作区 pages/ 目录内");
  }
  return target;
}

function text(content: string, details?: unknown) {
  return { content: [{ type: "text" as const, text: content }], details: details ?? undefined };
}

// ---- 扩展入口 ----

export default function (pi: ExtensionAPI) {
  // ---------- 记忆工具 ----------

  pi.registerTool({
    name: "memory_search",
    label: "记忆检索",
    description:
      "在工作区共享记忆（event_memory）中检索与 query 相关的历史事件并挂载。miss 时自动回退为时间坐标浏览列表，可用 memory_pick 按序号挂载。",
    promptSnippet: "memory_search - 在工作区长期记忆中检索历史事件",
    parameters: Type.Object({
      query: Type.String({ description: "检索语句（会自动提取关键词）" }),
    }),
    execute: async (_id, params) => {
      await ensureSidecar();
      const res = await api("/retrieve_full", { query: params.query });
      const mounted = res.mounted || [];
      if (mounted.length) {
        return text(
          `找到 ${mounted.length} 条相关记忆：\n\n${mounted.map((m: any) => m.content).join("\n\n")}`,
          { count: mounted.length },
        );
      }
      const b = await api("/browse", { n: 10 });
      return text(
        b.listing
          ? `无直接相关记忆。以下是最近事件（时间坐标），可用 memory_pick 序号挂载：\n${b.listing}`
          : "记忆为空。",
        { count: 0 },
      );
    },
  });

  pi.registerTool({
    name: "memory_browse",
    label: "记忆浏览",
    description: "按时间倒序列出工作区记忆（序号 + 日期 + 摘要），配合 memory_pick 使用。",
    promptSnippet: "memory_browse - 按时间列出全部历史事件",
    parameters: Type.Object({
      n: Type.Optional(Type.Number({ description: "条数，默认 20" })),
    }),
    execute: async (_id, params) => {
      await ensureSidecar();
      const res = await api("/browse", { n: params.n ?? 20 });
      return text(res.listing || "记忆为空。", res.items ?? []);
    },
  });

  pi.registerTool({
    name: "memory_pick",
    label: "记忆挂载",
    description: "按 memory_browse 返回的序号挂载对应事件的完整原文。",
    promptSnippet: "memory_pick - 按序号挂载历史事件原文",
    parameters: Type.Object({
      event_no: Type.Number({ description: "memory_browse 返回的序号" }),
    }),
    execute: async (_id, params) => {
      await ensureSidecar();
      const res = await api("/pick", { event_no: params.event_no });
      return text(res.content, { event_id: res.event_id });
    },
  });

  pi.registerTool({
    name: "memory_save",
    label: "记忆保存",
    description:
      "把一段已完成的重要事实/结论/决定写入工作区共享记忆（跨会话可检索）。text 会按内容寻址去重。",
    promptSnippet: "memory_save - 把重要结论写入长期记忆",
    promptGuidelines: [
      "完成重要任务、得出关键结论、或用户明确要求记住时，用 memory_save 写入记忆",
    ],
    parameters: Type.Object({
      text: Type.String({ description: "要记住的完整原文" }),
      hard_keys: Type.Optional(Type.Array(Type.String(), { description: "硬关键词（实体/文件名/专有名词，AND 检索用）" })),
      soft_keys: Type.Optional(Type.Array(Type.String(), { description: "软关键词（主题/意图/动作，打分用）" })),
      summary: Type.Optional(Type.String({ description: "一句话摘要" })),
    }),
    execute: async (_id, params) => {
      await ensureSidecar();
      const res = await api("/add_event", {
        text: params.text,
        hard_keys: params.hard_keys ?? [],
        soft_keys: params.soft_keys ?? [],
        summary: params.summary ?? "",
      });
      return text(`已写入记忆：${res.event_id}`, res);
    },
  });

  // ---------- 工作区页面工具（仅限 workspace/pages/） ----------

  pi.registerTool({
    name: "page_create",
    label: "创建页面",
    description: `在工作区 pages/ 目录创建 Markdown 页面（已存在时需 overwrite=true）。只能用纯文件名，不允许任何路径分隔符。`,
    promptSnippet: "page_create - 在工作区创建 Markdown 页面",
    parameters: Type.Object({
      name: Type.String({ description: "页面名（不含 .md 后缀，禁止 / \\ ..）" }),
      content: Type.String({ description: "Markdown 正文" }),
      overwrite: Type.Optional(Type.Boolean({ description: "已存在时是否覆盖，默认 false" })),
    }),
    execute: async (_id, params) => {
      const target = pagePath(params.name);
      fs.mkdirSync(PAGES_DIR, { recursive: true });
      const exists = fs.existsSync(target);
      if (exists && !params.overwrite) {
        return { ...text(`页面 ${params.name}.md 已存在。如需覆盖请带 overwrite=true。`), isError: true };
      }
      fs.writeFileSync(target, params.content, "utf-8");
      return text(`已${exists ? "覆盖" : "创建"}页面：${target}`, { path: target });
    },
  });

  pi.registerTool({
    name: "page_read",
    label: "读取页面",
    description: "读取工作区 pages/ 内的一个 Markdown 页面。",
    parameters: Type.Object({
      name: Type.String({ description: "页面名（不含 .md 后缀）" }),
    }),
    execute: async (_id, params) => {
      const target = pagePath(params.name);
      if (!fs.existsSync(target)) {
        return { ...text(`页面 ${params.name}.md 不存在。`), isError: true };
      }
      return text(fs.readFileSync(target, "utf-8"), { path: target });
    },
  });

  pi.registerTool({
    name: "page_delete",
    label: "删除页面",
    description: "删除工作区 pages/ 内的一个页面（不可恢复，仅限工作区内）。",
    parameters: Type.Object({
      name: Type.String({ description: "页面名（不含 .md 后缀）" }),
    }),
    execute: async (_id, params) => {
      const target = pagePath(params.name);
      if (!fs.existsSync(target)) {
        return { ...text(`页面 ${params.name}.md 不存在。`), isError: true };
      }
      fs.unlinkSync(target);
      return text(`已删除页面：${target}`, { deleted: target });
    },
  });

  pi.registerTool({
    name: "page_list",
    label: "页面列表",
    description: "列出工作区 pages/ 内的全部页面（名字 + 大小 + 修改时间）。",
    parameters: Type.Object({}),
    execute: async () => {
      if (!fs.existsSync(PAGES_DIR)) return text("pages 目录为空。", []);
      const files = fs
        .readdirSync(PAGES_DIR)
        .filter((f) => f.endsWith(".md") && fs.statSync(path.join(PAGES_DIR, f)).isFile());
      if (!files.length) return text("pages 目录为空。", []);
      const lines = files.map((f) => {
        const st = fs.statSync(path.join(PAGES_DIR, f));
        return `${f}  ${st.size}B  ${st.mtime.toISOString().slice(0, 19).replace("T", " ")}`;
      });
      return text(lines.join("\n"), files);
    },
  });

  // ---------- 会话启动：自动挂载相关记忆 ----------

  pi.on("before_agent_start", async (event) => {
    const q = (event.prompt || "").trim();
    if (!q) return;
    try {
      await ensureSidecar();
      const res = await api("/retrieve_full", { query: q });
      const mounted = res.mounted || [];
      if (mounted.length) {
        const block =
          "[工作区记忆] 以下是按当前任务检索到的历史事件，它们不是当前对话流的一部分：\n\n" +
          mounted.map((m: any) => m.content).join("\n\n") +
          "\n[记忆结束]";
        return { systemPrompt: `${event.systemPrompt}\n\n${block}` };
      }
    } catch {
      /* 记忆系统故障不拖死主流程 */
    }
  });

  // ---------- 越界拦截：内置 write/edit 仅限工作区 ----------

  pi.on("tool_call", async (event) => {
    if (event.toolName !== "write" && event.toolName !== "edit") return;
    const input = (event.input || {}) as Record<string, unknown>;
    const raw = String(input.path || input.file_path || input.filePath || "");
    if (!raw) return;
    const abs = path.isAbsolute(raw) ? path.resolve(raw) : path.resolve(process.cwd(), raw);
    const wsAbs = path.resolve(WORKSPACE);
    const pkgAbs = path.resolve(PKG_ROOT);
    // 允许写：工作区内；以及包内（扩展自身/服务端所需）。
    const allowed = abs.startsWith(wsAbs + path.sep) || abs.startsWith(pkgAbs + path.sep);
    if (!allowed) {
      return { block: true, reason: `pi-event-memory 扩展：写文件仅限工作区 ${WORKSPACE}，已拦截 ${abs}` };
    }
  });

  // ---------- 自动归档：compaction 时把对话原文写入工作区记忆 ----------
  // pi 决定压缩上下文时，先把被折叠的对话原样归档进 event_memory
  // （entry id 去重 + 内容寻址去重双保险，重复归档无害），
  // 之后模型只能看到 pi 的摘要，原文永远可经 memory_search/pick 找回。

  const archivedEntryIds = new Set<string>();

  function flattenText(content: unknown): string {
    if (typeof content === "string") return content.trim();
    if (Array.isArray(content)) {
      return content
        .map((b: any) => (b && b.type === "text" && typeof b.text === "string" ? b.text : ""))
        .filter(Boolean)
        .join("\n")
        .trim();
    }
    return "";
  }

  async function archiveEntries(entries: any[]): Promise<void> {
    const messages: { role: string; content: string }[] = [];
    for (const entry of entries) {
      if (!entry || entry.type !== "message" || archivedEntryIds.has(entry.id)) continue;
      const msg = entry.message;
      if (!msg || (msg.role !== "user" && msg.role !== "assistant")) continue;
      const text = flattenText(msg.content);
      if (!text) continue;
      archivedEntryIds.add(entry.id);
      messages.push({ role: msg.role, content: text });
    }
    if (messages.length) {
      await ensureSidecar();
      await api("/compress", { messages });
    }
  }

  pi.on("session_before_compact", async (event) => {
    try {
      await archiveEntries(event.branchEntries as any[]);
    } catch {
      /* 归档失败不阻塞 pi 的 compaction */
    }
  });

  // 会话真正关闭（退出/开新会话）时，把最后一段未压缩的对话 seal 落盘。
  pi.on("session_shutdown", async (event, ctx: any) => {
    if (event.reason === "quit" || event.reason === "new") {
      try {
        const branch = ctx?.sessionManager?.getBranch?.() ?? [];
        await archiveEntries(branch as any[]);
      } catch {
        /* 尽力而为 */
      }
    }
    stopSidecar();
  });
  process.once("exit", stopSidecar);

  // ---------- /memory 命令：查看记忆统计 ----------

  pi.registerCommand("memory", {
    description: "查看工作区记忆统计",
    handler: async (_args, ctx) => {
      try {
        await ensureSidecar();
        const s = await api("/stats");
        const msg = `工作区记忆（${WORKSPACE}）：${s.events} 事件 / ${s.keywords} 关键词 / ${s.postings} postings / 热缓存 ${s.hotcache_entries}`;
        ctx.ui.notify(msg, "info");
      } catch (e: any) {
        ctx.ui.notify(`sidecar 不可用：${e?.message ?? e}`, "warning");
      }
    },
  });
}
