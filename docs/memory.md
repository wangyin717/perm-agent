# Memory

跨会话记忆。参考 Grok Build 的三层，落盘全在 `~/.spark-agent`，不写进用户仓库。

路径、`memory_search`、Flush（回合停稳）、Dream（过门后覆盖项目 MEMORY.md）已落地。v1 召回只有一个工具。TUI 闲时定时器未接。

jsonl 不是 memory。压缩摘要也不是 memory。`AGENTS.md` 是人写的项目说明书，开场已经拼进 system，不要再叫 memory。

---

## 三层

| 层 | 是什么 | 文件 | 谁写 | 谁读 |
|---|---|---|---|---|
| 短期 | 本轮对话 | `chats/{sessionId}/session.jsonl` | loop | 本 session 投影 |
| 中期 | 这一次聊里可复用的事实 | `memory/YYYY-MM-DD-slug-{sessionId}.md` | Flush | Dream、`memory_search` |
| 长期（项目） | 这个仓库仍然成立的约定 | `memory/MEMORY.md` | Dream（整份覆盖） | `memory_search` / `memory_get` |
| 长期（全局） | 跨项目的人的偏好 | `~/.spark-agent/MEMORY.md` | 人改或工具写；v1 Dream 不自动改 | 同上 |

短期跟**这一次聊天**走，在 `{sessionId}/` 里。  
中期和项目长期跟**这个仓库**走，和所有 `{sessionId}` 平级，都在 `memory/`。  
不要把 `MEMORY.md` 放进某一个 `chats/{sessionId}/`。

---

## 目录

`SPARK_AGENT_HOME` 可改根，默认 `~/.spark-agent`（创建时尽量 `chmod 700`）。`{slug}-{hash}` 见 `paths.py`：目录名 + 仓库绝对路径 sha256 前 8 位。

```text
~/.spark-agent/MEMORY.md

~/.spark-agent/projects/{slug}-{hash}/
  memory/MEMORY.md
  memory/YYYY-MM-DD-slug-{sessionId}.md
  chats/{sessionId}/session.jsonl
  chats/{sessionId}/workspace/tools_result/…
```

每次 Flush / Dream 往 `memory/activity.jsonl` 追加一行（`no_reply` / `wrote` / `error`）。Dream 因门没过不写这一行，避免每轮刷屏。TUI 和 CLI 也会打 `[memory:flush] no_reply` 这类过程日志。

`memory/` 里靠文件名区分：

- `MEMORY.md`：项目长期  
- `YYYY-MM-DD-*.md`：中期  

列中期时用日期前缀过滤，不要 `*.md` 一把抓（会把自己的 `MEMORY.md` 当流水账）。  
`memory_search` 只扫 `memory/` + 全局 `MEMORY.md`，**不要扫 `chats/`**。

---

## Flush（写中期）

另打一枪模型，不是压缩。

压缩：把本轮窗口里的旧对话换成短摘要，写进 jsonl，给**下一枪同一个 session**。  
Flush：从刚停稳的对话里抽出「以后还用得上的」，写成中期 markdown。

**何时：** 一次用户问题完整停稳之后（内层没有 tool、也没有 follow_up）。不是每个 tool_result 之后。v1 不必等 TUI 空闲定时器。

**写到哪：**  
`projects/{slug}-{hash}/memory/YYYY-MM-DD-{slug}-{sessionId}.md`

- `YYYY-MM-DD`：当天日期  
- `slug`：用户第一句话压成短、文件系统安全的一小段  
- `sessionId`：与 `chats/` 下目录名相同  

同一 session 再 Flush 一次：**还是这个文件**，用 `---` 和 `<!-- flush HH:MM -->` append，不要新开日历文件。

**正文：** 必须带 `##` 主题标题（没有 `##` 整份丢掉）。短：每节最多 3 条、全文最多 12 条，目标 ≤1500 字，写盘硬顶 2000。空主题整节省略，不要四个标题都凑上。主题对齐 Grok：

- Decisions & rationale  
- Technical context（架构、API、仓库相对路径）  
- Debugging techniques  
- Problems & solutions  

不要写 Current state / Next steps（那是压缩摘要）。不要写 OS/编辑器等偏好（那是全局 `MEMORY.md`）。不要复述工具过程。

**`NO_REPLY`：** 提示词要求：例行问答、没新决定、没新发现，模型只回复 `NO_REPLY`。程序看到空、就是 `NO_REPLY`、或没有 `##` → **文件不写、不 append**。

Flush 和压缩摘要必须两套提示词、两处落盘，不能共用。

---

## Dream（收成项目长期）

另打一枪模型。默认 **只更新项目** `memory/MEMORY.md`，不是两次模型（不必用同一堆中期再生成全局）。

**输入：** 近期 `YYYY-MM-DD-*.md` + 现有项目 `MEMORY.md`。  
**输出：** 一整份新的 markdown **覆盖** `memory/MEMORY.md`（合并、改掉过时事实、丢掉流水）。不是 append。有字数上限，超了就在这一次生成时压条目。

**提示词要点（对齐 Grok dream）：**

- 相关的合成一个主题  
- 新事实推翻旧的，只留现在为真的  
- 「昨天」改成绝对日期  
- 丢掉问候、工具噪音、Current state、Next steps、已在全局里的偏好  
- 留下决定、架构、问题/修法  
- 没什么可留就 `NO_REPLY`，长期文件不动  

**全局** `~/.spark-agent/MEMORY.md`：v1 靠人改或以后的 memory 工具写。不要每闲一次用同一堆中期再 dream 一遍全局。

**何时跑：** 不是系统守护进程。Grok 是 TUI 会话循环里的定时器 + 启动时检查；关会话不跑 Dream。

我们现在 CLI 跑完就退出，v1：

- 进程 `return` 前过门再试一次；或  
- 单独命令（人闲了自己跑）

门（过不了就不打模型）：距上次成功 Dream 够小时数；上次之后新中期文件够份数；文件锁（两个窗口不抢同一项目）。以后有常驻 TUI，再加循环里的定时器。

---

## 召回

两个工具，模型需要时再调。开场 **不** 自动把中期/长期灌进 system（AGENTS.md + cwd + 日期已经在）。压缩后再搜一次（Grok 第三条）v1 不做。

v1 **一个工具** `memory_search`：

- `query`：AND 关键词，按 `##` 切块，搜项目 MEMORY.md → 中期（新的在前）→ 全局  
- `path`：短名读一份（`MEMORY.md` / `global/MEMORY.md` / `YYYY-MM-DD-*.md`）；可加 `offset`/`limit`  
- 两个都有：只在该文件里搜  
- 两个都空：拦住  

短名不要拼家目录绝对路径。不出 `memory/` 沙箱。默认最多 8 条。不上 FTS/向量。

---

## 和现有模块的边界

| 已有 | 不要当成 memory |
|---|---|
| jsonl / 压缩摘要 | 本 session 窗口 |
| `AGENTS.md` 拼进 system | 人写的项目说明书 |
| grep/fetch 快照 | 这一次工具原文，在 `chats/{id}/workspace/tools_result/` |

| 以后才做 | 说明 |
|---|---|
| TUI 闲时定时 Flush/Dream | memory v1 不依赖 |
| 开场自动注入 / 压缩后补召回 | 需要时再加 |
| FTS / 向量 | 文件很多、关键词对不上再换引擎；md 格式保持不变就能换 |

---

## 建议实现顺序

1. 路径、`memory_search`、Flush、Dream 已接进 `_run_loop` 收尾  
2. 以后 TUI 闲时定时器；可选单独 `dream` 命令  
3. FTS / 开场注入 / 压缩后补召回 — 需要时再加
