# pi-event-memory（for every love）

一个标准 **pi package**：给 [pi coding agent](https://github.com/badlogic/pi-mono) 加上**工作区事件记忆（event_memory）**与**工作区页面管理**。记忆系统完全由代码管理（倒排索引 + 原文无损落盘 + 分级降级检索），模型只拥有工具触发权。

中文 | [English](README.en.md)

## 包结构（本仓库即包）

```
package.json            pi 清单（pi-package keyword，pi.extensions 指向 extensions/）
extensions/for-every-love.ts   扩展本体
agent/server.py         event_memory HTTP sidecar（127.0.0.1:8766，仅本机）
event_memory/           Python 记忆库（事件切分 + 倒排索引 + 原文无损落盘）
agent/workspace/        本仓库内使用时的默认工作区（memory/ + pages/）
```

## 安装

```bash
# 本地路径（即装即用，不复制文件）
pi install "/path/to/for every love"

# git
pi install git:github.com/fiaselya/for-every-love

# npm（可选：npm publish，包名 pi-event-memory）
pi install npm:pi-event-memory
```

个人级安装写入 `~/.pi/agent/settings.json`，所有项目可用；`pi install -l` 则只写入当前项目的 `.pi/settings.json`（需项目信任）。`pi list` 查看，`pi remove` 卸载。

## 工作区与路径约定

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `FEL_WORKSPACE` | `<cwd>/agent/workspace` | 每个项目一个独立记忆工作区（memory/ + pages/） |
| `FEL_PORT` | `8766` | sidecar 端口 |
| `FEL_PYTHON` | 包内 `.venv/bin/python` → 系统 `python3` | sidecar 解释器 |
| `FEL_LLM_CONFIG` | `<包根>/llm_config.json` | 记忆内务 LLM（缺文件或 httpx 未装则自动 llm=None 降级，示例见 `llm_config.example.json`） |

sidecar 由扩展按需自动拉起（仅监听 127.0.0.1），会话结束自动关闭。

## Agent 获得的工具

| 工具 | 说明 |
|---|---|
| memory_search | 检索工作区记忆并挂载（miss 自动回退时间坐标列表） |
| memory_browse / memory_pick | 按时间浏览 / 按序号挂载原文 |
| memory_save | 写入跨会话记忆（内容寻址去重） |
| page_create / page_read / page_delete / page_list | 工作区页面管理（硬性限制在 pages/ 内，禁 `..` `/` `\`） |
| /memory | 命令：查看记忆统计 |

自动行为（全部由代码执行，模型只有工具触发权）：
- 任务开始：自动检索相关记忆注入 system prompt。
- compaction 前：把被折叠的对话原文自动归档进记忆（原文永久可找回）。
- 会话结束（quit/new）：seal 最后一段对话。
- 内置 write/edit 被拦截为「仅限工作区」。

## 依赖

- pi ≥ 0.87（`@earendil-works/pi-coding-agent`）
- Python 3（sidecar；记忆本体纯标准库，`httpx` 可选——没装则记忆内务 LLM 自动降级）

## 设计原则

**上下文由编排层管理，模型不管理。** 检索、挂载、截断、去重、归档全部在代码侧强制执行；模型只能按工具按钮（memory_save / memory_search），按钮背后的机制碰不到。误触发的最坏后果是多存一条事件（内容寻址去重兜底），永远不会破坏索引或已有记忆——「宁可不命中，不要错命中」。

## 许可证

MIT
