# Agent Loop

这套 loop 参考 pi harness-v2：本地可跑、jsonl 可对账。  
还不是完整 pi（没有多 lane、SQLite）。官方插件目前只有 browser-use。

加工具：写 `execute` + `REPLAY` + `READ_ONLY` + 自己的 hooks，登记进 `TOOLS`，不必改循环。增删工具只改 registry + schema；人设里的工具注意事项按启用工具再拼，不要在 yaml 里抄参数表。

密钥：仓库根 `.env` 或 `~/.permanent/.env`（`DEEPSEEK_API_KEY`、`PERPLEXITY_API_KEY`，Jev 用 `TYPESAFE_API_KEY`），启动时 `envfile.load_dotenv()` 读入；已有环境变量不覆盖。`.env` 不进 git，`.env.example` 只有变量名。Jev 三处见 `docs/jev.md`。开发入口：`uv sync && uv run perm`。对外安装：`install.sh` 把代码和 venv 放到 `~/.permanent/src`，uv 管理的 Python 在 `~/.permanent/python`，browser-use 的隔离环境在 `~/.permanent/uv-tools`，命令是 `perm`（包装脚本会把 `~/.permanent/bin` 加进 PATH）；`perm update` 换到新的 `v*` tag。只有这份托管安装才在启动时检查新 tag。

跨会话 memory 见 §4。

---

## 分层（现在 vs 以后）

可以按四层想，不要并列两套「核心」：

| 层 | 现在 | 以后 |
|---|---|---|
| 模型 | `llm/deepseek.py`：`call(messages, tools)` | 再加适配器 |
| 核心 | loop + runtime + jsonl + inbox / 压缩 / 恢复 | 尽量不塞具体工具实现 |
| 界面 | `cli/tui.py`、`cli/trace.py`：只订 `events`，不改 jsonl | 还可有 headless |
| 扩展 | 官方 browser-use 插件：SKILL.md + 会话进程里的两个工具 | 再加别的插件 |

`tools/` + `prompt/` + `memory/` 是 **这个 coding agent 的产品能力**，和 loop 一起发布。循环真正必备的是「能调工具、能带 system」，不是必备 bash 这一份实现。现阶段一个包，不必拆成两个 Python 包。

权威状态是 jsonl。TUI 是订阅者。

---

## 1. React loop

入口：CLI / TUI 走 `ReactAgentLoop._run_loop`。`reply(memory, …)` 还在，是 SwiftAgent 时期的外壳，本仓库不用。

```text
读 jsonl，apply_recovery（先给上一世收尸）
再 inspect 一次
continue_llm → 本轮新话进 inbox.steer（插队）
其它该写 user → append user
messages = [system] + jsonl 的 entry 投影（已应用 redaction / summary）

外层 while:
  内层 while (还有 tool 或 steer):
    若上一枪已 end_turn、接下来要吃 steer → 按 60% 压（新回合）
    drain steer → 写 user entry + 推进 messages
    写 step_attempt（或复用未关的 assistant id）
    仅当下一枪估算 ≥100% 窗口 → 压（回合中途保命）
    llm.call
    end_turn / 无 tool_calls → 写 assistant，内层可停
    否则跑工具（准备串行、执行按路径锁并发），tool result 进 messages
  内层停稳 = 回合结束 → 按 60% 压
  drain follow_up，变成 steer，再开一圈内层
  没有 follow_up → return
```

LLM 每步只有两种出口：`end_turn` 或 `tool_use`。没有 `MAX_STEPS`，和 pi 一样靠模型停；abort / 工具 `terminate` 可提前停（`terminate` 只截断喂给模型的投影，jsonl 仍记下所有已执行的结果）。

人设由 `prompt/assemble.py` 每次拼：

```text
yaml 行为稿
+ 已启用工具的 tool_notes（见 §10）
+ Skills 短名单（name + description + SKILL.md 路径；全文用 read）
+ 仓库根 AGENTS.md（若有，超过 8k 截断）
+ Current working directory
+ Today's date
```

参数、用法写在 schema。yaml 只留人设和「重叠工具怎么选」。skill 名单由 `prompt/skills.py` 扫描后接在 tool_notes 和 AGENTS.md 之间。出厂 skill 若还有，在 `bundled_skills/`，缺的才拷到 `~/.permanent/skills/`。官方 **browser-use 插件**在包内 `plugins/browser-use/`（`SKILL.md` + `plugin.json`），缺的才拷到 `~/.permanent/plugins/browser-use/`。system 里的 `File:` 指向家目录这份，再用 `read`。仓库 `.permanent/skills/` 或 `.permanent/plugins/` 同名覆盖。插件默认开（`config.json` 里 `plugins.browser-use`）。开 TUI 即起 `browser-use --cli-mcp`，把 `browser_exec` / `browser_screenshot` 放进这次 `llm.call` 的 schema。连不上时同一条 `browser_exec` 打开检查页并等 Allow，点完自动继续。

---

## 2. 内层插队 / 外层排队

`inbox.py`：`UserInbox`

| | 方法 | 何时进模型 |
|---|---|---|
| **插队 steer** | `loop.inbox.push_steer(text, media=None)` | 当前 step 结束之后（tool result 已进 messages）、下一次 `llm.call` 之前 |
| **排队 follow_up** | `loop.inbox.push_follow_up(text)` | 内层认为可以停了（没 tool、也没待插队）再进 |

不会在 `execute` 半截插入。崩溃恢复后若是 `continue_llm`（差最终回复），这次 HTTP 的新 user **走插队**，不再丢掉（例如「用英文回复」下一枪 LLM 能看见）。

「请继续」更像 follow_up；「请继续，用英文」更像 steer。

end_turn 之后才进的 steer 当成**新回合**：先按 60% 做压缩，再注入。

---

## 3. 工具 hook

按工具名分发。

```text
查表 get_tool
  → before_tool     可改 args，或 block
  → tool_started    盖章：effective_args + result_id + replay
  → execute         必须用 started.effective_args；可收 workspace、session_dir
  → after_tool      可改 content / is_error / terminate
  → tool_result     同一 result_id 关单
```

- hook 只返回意见，**不准** `messages.append`
- `block` / 未知工具：**不写** started，直接 `create_error_tool_result`
- 超长输出：**不要**用 after 硬切再丢掉后半段。read / grep / bash 在 **execute** 里翻页或落盘（见 §9、§11）。after 现在：

  | 工具 | after hook |
  |---|---|
  | bash / read / grep / edit / write | 无（空列表） |
  | web_search | 32k |
  | web_fetch | 64k（落盘后回给模型的是短预览，一般碰不到） |

- bash：另有 `before_tool_deny_rm`；edit：空 `old` 在 before_tool 拦住。Jev 放行（bash / write / edit）接在硬规则之后，见 `docs/jev.md`

加工具：`NAME` / `REPLAY` / `READ_ONLY` / `BEFORE_HOOKS` / `AFTER_HOOKS` / `execute` → `tools/registry.py`，并在 `tool_schemas.json` 加 schema。理想情况 schema 跟 registry 一份清单；现在仍是两处，增删要一起改。

当前工具：`bash` / `read` / `write` / `edit` / `grep` / `web_search` / `web_fetch` / `memory_search`。

落盘「给模型看之前」这个时机对，但 **after_tool 以现在的形状不能当所有工具的唯一落盘点**（没有 `session_dir`；grep 的 cursor 快照也不是截断）。blob 类（fetch / 长抽取 / bash）在 execute 末尾调同一类「预览 + 写 `tools_result`」；grep cursor、read 文本 offset 留在各自 execute。以后若要通用 Truncate，再给 after 补 `session_dir`，并且仍盖不住 grep / 普通 read。

---

## 3.1 工具并行

一批 `tool_calls` 分两阶段（`runtime/tool_concurrency.py`）：

```text
阶段一（严格串行）  逐个 before_tool + 写 tool_started
                   unknown / blocked 直接产出错误结果，不进阶段二
阶段二（并发）      READ_ONLY=False 且有 path 的调用，按解析后的绝对路径建锁
                   任何调用命中这些写路径就排队；其余直接并发
                   asyncio.gather 等完，返回顺序 = 原始调用顺序
```

| 工具 | `READ_ONLY` | 锁 |
|---|---|---|
| read / grep / memory_search | True | 有 `path` 才参与；撞上本批某条写路径才排队 |
| write / edit | False | 按解析后的绝对路径字符串全等加锁（不做目录包含） |
| bash | False | 共用 `__bash__`，同一批里 bash 彼此串行；不解析 cmd，也不和 edit/write 的文件 path 互斥 |
| web_search / web_fetch | 无仓库 path | 不加锁 |

`terminate`：一批都跑完。jsonl 照实记录所有 `tool_result`。喂给模型的 messages / `entries_to_messages` 投影按 `tool_calls` 顺序排，截到第一条 `terminate=True`，并把该条 assistant 的 `tool_calls` 裁到与保留的 result 对齐。恢复仍只看工单关没关，不认 terminate。

---

## 4. 家目录（会话 + 记忆）

不写进用户仓库。`PERMANENT_HOME` 可改根，默认 `~/.permanent`（创建时尽量 `chmod 700`）。`{slug}-{hash}` 见 `paths.py`：目录名 + 仓库绝对路径 sha256 前 8 位。同一路径稳定，不进 git。

jsonl 不是 memory。压缩摘要也不是 memory。`AGENTS.md` 是人写的项目说明书，开场已经拼进 system，不要再叫 memory。

```text
~/.permanent/MEMORY.md                      全局长期

~/.permanent/projects/{slug}-{hash}/
  memory/MEMORY.md                            项目长期（Dream 覆盖）
  memory/YYYY-MM-DD-slug-{sessionId}.md       Flush 中期
  memory/activity.jsonl
  chats/{sessionId}/session.jsonl             本轮对话
  chats/{sessionId}/title.json                显示名（目录名仍是 id）
  chats/{sessionId}/workspace/tools_result/
    grep/  web_fetch/  read/  bash/           草稿纸，不进 jsonl
```

草稿纸在会话目录下，**不进 jsonl**，不进模型投影。`ToolRuntime.session_dir` = jsonl 的父目录。`workspace` 仍是用户仓库根，搜代码用这个。

fetch / 长文档 / 长 bash 的 `saved:` 多为家目录绝对路径（相对 workspace 算不出来时）；grep / read 那条路径时用 `path=` 指向该文件。

### 4.1 三层

| 层 | 是什么 | 文件 | 谁写 | 谁读 |
|---|---|---|---|---|
| 短期 | 本轮对话 | `chats/{sessionId}/session.jsonl` | loop | 本 session 投影 |
| 中期 | 这一次聊里可复用的事实 | `memory/YYYY-MM-DD-slug-{sessionId}.md` | Flush | Dream、`memory_search` |
| 长期（项目） | 这个仓库仍然成立的约定 | `memory/MEMORY.md` | Dream（整份覆盖） | `memory_search` / `memory_get` |
| 长期（全局） | 跨项目的人的偏好 | `~/.permanent/MEMORY.md` | 人改或工具写；v1 Dream 不自动改 | 同上 |

短期跟**这一次聊天**走，在 `{sessionId}/` 里。  
中期和项目长期跟**这个仓库**走，和所有 `{sessionId}` 平级，都在 `memory/`。  
不要把 `MEMORY.md` 放进某一个 `chats/{sessionId}/`。

`memory/` 里靠文件名区分：`MEMORY.md` 是项目长期，`YYYY-MM-DD-*.md` 是中期。列中期时用日期前缀过滤，不要 `*.md` 一把抓。`memory_search` 只扫 `memory/` + 全局 `MEMORY.md`，**不要扫 `chats/`**。

每次 Flush / Dream 往 `memory/activity.jsonl` 追加一行（`no_reply` / `wrote` / `error`）。Dream 因门没过不写这一行。TUI 和 CLI 也会打 `[memory:flush] no_reply` 这类过程日志。

### 4.2 Flush（写中期）

另打一枪模型，不是压缩。压缩把本轮窗口里的旧对话换成短摘要，写进 jsonl，给**下一枪同一个 session**。Flush 从刚停稳的对话里抽出「以后还用得上的」，写成中期 markdown。

**何时：** 一次用户问题完整停稳之后（内层没有 tool、也没有 follow_up）。不是每个 tool_result 之后。v1 不必等 TUI 空闲定时器。

**写到哪：** `projects/{slug}-{hash}/memory/YYYY-MM-DD-{slug}-{sessionId}.md`

- `YYYY-MM-DD`：当天日期
- `slug`：用户第一句话压成短、文件系统安全的一小段
- `sessionId`：与 `chats/` 下目录名相同

同一 session 再 Flush 一次：**还是这个文件**，用 `---` 隔开再 append，不要新开日历文件。每次写成（含第一次）都带时间戳：`<!-- flush 20260917 17:41 -->`。

**正文：** 必须带 `##` 主题标题（没有 `##` 整份丢掉）。短：每节最多 3 条、全文最多 12 条，目标 ≤1500 字，写盘硬顶 2000。空主题整节省略。主题对齐 Grok：Decisions & rationale、Technical context、Debugging techniques、Problems & solutions。不要写 Current state / Next steps（那是压缩摘要）。不要写 OS/编辑器等偏好（那是全局 `MEMORY.md`）。不要复述工具过程。

**`NO_REPLY`：** 例行问答、没新决定、没新发现，模型只回复 `NO_REPLY`。程序看到空、就是 `NO_REPLY`、或没有 `##` → **文件不写、不 append**。

Flush 和压缩摘要必须两套提示词、两处落盘，不能共用。

### 4.3 Dream（收成项目长期）

另打一枪模型。默认 **只更新项目** `memory/MEMORY.md`，不是两次模型。

**输入：** 近期 `YYYY-MM-DD-*.md` + 现有项目 `MEMORY.md`。  
**输出：** 一整份新的 markdown **覆盖** `memory/MEMORY.md`（合并、改掉过时事实、丢掉流水）。不是 append。有字数上限，超了就在这一次生成时压条目。

提示词要点：相关的合成一个主题；新事实推翻旧的；「昨天」改成绝对日期；丢掉问候、工具噪音、Current state、Next steps、已在全局里的偏好；留下决定、架构、问题/修法；没什么可留就 `NO_REPLY`，长期文件不动。

全局 `~/.permanent/MEMORY.md`：v1 靠人改或以后的 memory 工具写。不要每闲一次用同一堆中期再 dream 一遍全局。

**何时跑：** 不是系统守护进程。CLI 跑完就退出，v1 在进程 `return` 前过门再试一次。门：距上次成功 Dream 够小时数；上次之后新中期文件够份数；文件锁。关会话不跑 Dream。以后有常驻 TUI，再加循环里的定时器。

### 4.4 召回

两个工具，模型需要时再调。开场 **不** 自动把中期/长期灌进 system（AGENTS.md + cwd + 日期已经在）。压缩后再搜一次 v1 不做。

v1 **一个工具** `memory_search`：

- `query`：AND 关键词，按 `##` 切块，搜项目 MEMORY.md → 中期（新的在前）→ 全局
- `path`：短名读一份（`MEMORY.md` / `global/MEMORY.md` / `YYYY-MM-DD-*.md`）；可加 `offset`/`limit`
- 两个都有：只在该文件里搜
- 两个都空：拦住

短名不要拼家目录绝对路径。不出 `memory/` 沙箱。默认最多 8 条。不上 FTS/向量。

### 4.5 和现有模块的边界

| 已有 | 不要当成 memory |
|---|---|
| jsonl / 压缩摘要 | 本 session 窗口 |
| `AGENTS.md` 拼进 system | 人写的项目说明书 |
| grep/fetch 快照 | 这一次工具原文，在 `chats/{id}/workspace/tools_result/` |

路径、`memory_search`、Flush（回合停稳）、Dream（过门后覆盖项目 MEMORY.md）已落地。v1 召回只有一个工具。TUI 闲时定时器未接。开场自动注入、压缩后补召回、FTS 需要时再加。

| kind | type | 是什么 | 进模型吗 |
|---|---|---|---|
| record | step_attempt | 即将打 LLM，预分配 assistant id | 否 |
| record | tool_started | 即将执行工具 | 否 |
| entry | user / assistant / tool_result | 对话 | 是（压缩切点之后的才投影） |
| entry | tool_result_redacted | micro 把某条 tool_result 换成占位符 | 否（投影时覆盖同 result_id 的正文） |
| entry | compaction_summary | summary 切点之前的摘要 | 否（投影成最前面一条 user） |

- `step_attempt.result_entry_id` = 随后那条 assistant 的 `id`
- `tool_started.result_id` = 随后那条 tool_result 的 `result_id`
- blocked / unknown 没有 started，result 自己生成 id
- `compaction_summary.covers_upto_seq`：这条 seq 及之前的可见 entry 不再进模型，改由摘要代表

权威是 jsonl。内存 list 只是本轮缓存。草稿纸只给工具自己读。

---

## 5. 上下文压缩

`compaction.py`。为了 KV / prompt cache：能追加就追加到回合结束，压缩是回合之间的换班，不是每枪手术。

记账格式（`tool_result_redacted` / `compaction_summary`）当契约冻住；进场比例可以再拧，但代码和文档要同一数字。

### 检查时机

| 时机 | 条件 | 在哪 |
|---|---|---|
| 回合结束 | `ratio ≥ 60%` | 内层停稳之后；或 end_turn 后改吃 steer 之前 |
| 回合中途 | `ratio ≥ 100%`（下一枪会超窗） | 每次 `llm.call` 之前，`overflow_only=True` |

低于 60% 不改已经发出去的前缀。中途 60%–100% 之间继续追加，让当前回合吃热 cache。

### 同一集 cascade

进场后做完再打主模型，中间不穿插主对话请求：

```text
≥ 触发线
  → microcompact（不打摘要 LLM）
  → 重投影 + 重算 ratio
  → ≤30% 收工
  → 仍 >30%：一次 summary（独立请求）
  → 仍 >30%：最后一轮太大，停止
```

| 常量 | 值 | 含义 |
|---|---|---|
| `TRIGGER_RATIO` | 60% | 回合结束进场 |
| `OVERFLOW_RATIO` | 100% | 中途保命 |
| `SUCCESS_RATIO` | 30% | 收工线 |
| `KEEP_TAIL_RATIO` | 10% | 最近这段完整轮次原样留（对齐到 user 边界，至少一轮） |

**microcompact**：只抠 keep 切点之外、`REPLAY==safe`、≥800 字符的旧 `read`/`grep`/`bash` 正文，换成可再调用的占位符。不删消息、不拆 tool 配对。write/edit（`REPLAY=never`）和短结果不动。micro 经常到不了 30%（骨架、最近 10%、tool_call arguments 都还在），这是预期；它给 summary 当 prelude。

**summary**：keep 切点之前送给独立摘要 LLM（系统提示是「摘要助手」，不带主对话、不带 tools），结构化输出 Goal / Progress / Key decisions / Files touched / Next steps。结果写成 `compaction_summary` 纯文本，换模型也能接着用。有上一次摘要则累积更新，不并排。

压完形态：`[system][tools][summary user][最近 10% 轮次]`。投影走 `entries_to_messages`，会丢掉切点之前的原文。

### token 估算（`ContextUsageTracker`）

`known_tokens` 用来在下一枪之前估 prompt 有多大。

- `update_from_response`：`prompt + completion` 覆盖。completion 就是即将进下一枪的 assistant。
- `add_estimate`：只补 completion 里没有的新消息（steer / tool_result）。**不要再估一遍 assistant**，否则 ratio 虚高。
- 压缩落盘后 `_reproject`：保留开头的 system，再 `bootstrap`。

`chars//4` 只填两次真实 usage 之间的空；下一枪 API usage 会校准。

---

## 6. 进程崩溃恢复

下次 `_run_loop` **开头**读 jsonl，不猜进程。当前进程里的异常走 `try/except`，不走 `recover.py`。

| 磁盘 | action | 现在会不会自动做 |
|---|---|---|
| started 无同 id 的 result | close_unfinished | **会**：两边 `replay==safe` 则重放；否则 `interrupted` |
| 最后是 assistant+tool_calls，还没 started | resume_tools | **会**：完整 `call_tool` |
| 有 step_attempt、没有同 id 的 assistant | （复用 id 再 `llm.call`） | **会**：`_begin_llm_step` 不 new id |
| 最后是 tool_result | continue_llm | **会**进 loop 再打模型；新 user 走 **steer** |
| 最后只有 user | start_llm | **会**投影后打模型；同一句 user 不重复写 |
| 最后是纯文本 assistant | idle | 无需恢复；新问题当本轮 user |

`replay` 要 **当时 jsonl 里的** 和 **现在代码里的 `REPLAY`** 都是 `safe` 才重放。bash / read / grep / web_search / web_fetch 目前 `safe`；write / edit 是 `never`。恢复重放会带上 `session_dir`。

Ctrl+C 是取消，不是崩溃：未关工具更合理写 interrupted，不要当 safe 重放。崩溃才是进程没了、只剩文件。

恢复后投影同样走 `entries_to_messages`，所以崩溃前落盘的 redaction / summary 下次进程还能看见。

`ToolRuntime` 里「先读再改」的快照（path → content + mtime）**只在内存**，不进 jsonl。恢复或新开一轮后表是空的，必须再 `read` 才能 `edit`。

---

## 7. LLM 失败：重试 vs step_attempt（不要混）

三件不同的「再试一次」：

| | 发生在哪 | jsonl |
|---|---|---|
| 超时/429/5xx 重试 | **`DeepSeekLLM.call` 内部** | 仍是同一张 `step_attempt` |
| 欠费 402 / 401 / 400 / 文案带 quota | 立刻抛，不重试 | 有 attempt、没 assistant |
| 进程死在 `llm.call` | 下次 `_begin_llm_step` 复用 id | 有 attempt、没 assistant |

`call()` 最多 3 次（第 1 次 + 再试 2 次）。退避约 0.5s → 1s → 2s（封顶 8s）；有 `Retry-After` 听它，超过 60s 直接失败。429 文案若像欠费（quota/billing）**不重试**。

Ctrl+C：`Abort` 打断退避和卡住的 POST（`RetryCancelledError`）。不新写 attempt。

密钥只从 `DEEPSEEK_API_KEY` 读，不再写死在 `deepseek.py`。

---

## 8. bash 错误

失败必须 **throw**，`ToolRuntime` 统一 `create_error_tool_result`（`is_error=true`），loop 继续，模型能看见。

默认本机 `bash -lc`。测试可传入带 `commands.run` 的 sandbox。

1. 没命令  
2. `exit_code != 0`  
3. 超时：同一 cmd 再跑 1 次；第二次还超时 → throw  

非 0 / 空 cmd **不重试**。

拼好 stdout+stderr 之后、return 或 raise **之前**，走同一套 `_preview_and_spill`（成功失败都算）。否则 pytest 栈会变成异常正文整份进模型。

活着失败：当场关单。只有进程死在 execute 里，才留给 `inspect_log`。

`asyncio.to_thread` + `wait_for`：本机 `subprocess.run(..., timeout=)` 超时会杀子进程；`wait_for` 仍杀不掉线程里卡住的调用。真要停远端沙箱里的命令，还没做（现在默认不走 E2B）。

---

## 9. 各工具要点

| 工具 | 要点 |
|---|---|
| read | 文本：`offset`（1-based）/`limit`，默认最多 1000 行或 10 万字节，footer 写下一页 `offset`。pdf/docx/xlsx 抽成文本（≤8k 内联，更长落盘）。jpeg/png/gif/webp 按文件头识别，jsonl 只记 `media.path`，并写一条不含像素的 `image_attached` record。投影时 tool 仍是文字，图挂在紧挨着的 `user`（DeepSeek 只允许 user 带图）。模型无视觉则 Pi 式省略说明。`REPLAY=safe` |
| write | 整文件；覆盖已有文件须先 read（见 §12）；新建不需要。`REPLAY=never`；缺目录 mkdir |
| edit | 精确字符串替换；空 old 在 before_tool 拦住；须先 read（分页也算）；成功后 TUI 画红绿 hunk。`REPLAY=never` |
| grep | 正则；第一页 20 条；完整命中写入 `tools_result/grep/<id>`；footer 带 cursor 则再调 grep 只传 cursor；无 `session_dir` 不写盘、不给 cursor |
| web_search | `query`，可选 `max_results`（默认 5，上限 10）。有 `PERPLEXITY_API_KEY` 走 Search API；否则（或 401/429/5xx/超时）走 DuckDuckGo 子进程。结果带 `[perplexity]` / `[duckduckgo]` / `[duckduckgo fallback]`。snippet 仍进 jsonl |
| web_fetch | 一次一个公开 https URL；拦内网；跳转每次再检查。全文写入 `tools_result/web_fetch/<id>`（UTF-8 文本：一行 `[web_fetch] url` 头 + 正文，不是纯 JSON）。tool_result 只留路径、字数、约 30 行预览。找页内文字用 grep/read 且 `path=` 该文件；不要 bash / 整文件 `json.load` |
| memory_search | 跨会话记忆。`query` 搜项目/中期/全局 md；`path` 读短名文件。不扫 jsonl |
| bash | 本机命令。超过 **2000 行或 50KB（UTF-8 字节）** 先到先停：全文写入 `tools_result/bash/<id>.txt`，模型看到前 200 行 + 后 1800 行。无 session 只做头尾、注明没存文件。不要用来读改搜文件或搜网/拉页；不要自己 `| head` / `| tail` |

web_search 的 DuckDuckGo 在独立子进程里跑 `ddgs`，30 秒杀不掉就 SIGKILL，避免卡住 loop。

UTF-8 字节是通用编码体积，不是中文专用：`len(s.encode("utf-8"))`。中文同等字符数会先碰到 50KB。

附件：文字型 pdf/docx/xlsx 走 `read` 抽取。jpeg/png/gif/webp 走 `read` 的图像路径（jsonl 不存 base64）。扫描 PDF、音视频仍不行。

---

## 10. 人设 vs 工具注意事项

`system_prompt.yaml`：

- `system_prompt`：人设、怎么干活、怎么写回复。回复默认先给结论再讲为什么；短段落和 `-` 列表。短表格可以，TUI 用 Unicode 方框画。不要抄 `cmd`/`offset` 这类参数。
- `tool_notes`：按工具名分段。`assemble` 对照 `TOOLS` 的插入顺序，**只把已启用的段**拼进 `# Tool notes`。从 registry 拿掉工具，对应段落不会再出现。

现在有笔记的：`bash`（专用工具优先、别 `find $HOME`、超长落盘）、`memory_search`、`edit`、`write`。

找文件：先看工作区、Desktop、Downloads、Documents；macOS 用 `mdfind -name`。禁止 `find $HOME` / `find ~`，也不要用 `-not -path '*/Library/*'` 当省事办法（会漏掉 iCloud）。

---

## 11. 超长结果：一种预算，不要两刀

| 机制 | 放哪 | 模型怎么续 |
|---|---|---|
| read 文本 | execute 按行切页 | `offset=N` |
| grep | execute 写命中快照 | `cursor=` |
| 长 pdf/docx/xlsx | execute 抽完 >8k 落盘 | grep/read 那个 txt |
| web_fetch | execute 默认落盘 | 同上 |
| bash | execute 行数或字节超限落盘 | grep/read 那个 txt；预览是头尾 |

不要 after 再切 8k：那会切掉页脚里的 offset/cursor。bash 也不要只留前 8k（失败栈在尾巴）。

Pi / OpenCode / Grok / Hermes 对 bash 几乎都是：**头或尾的预览 + 全文落盘 + 告诉模型路径**。我们对齐：2000 行或 50KB，前 200 + 后 1800，文件在 `tools_result/bash/`。

落盘阈值在内存里量已经拿到的字符串，不必先写盘再 `stat`。边跑边流式写文件（避免几百 MB 日志撑爆内存）还没做。

---

## 12. edit：精确替换 + 先读再改 + UI diff

算法和 Claude / Grok 同一条路：磁盘上精确匹配 `old`→`new`，默认唯一，否则报行号或要求 `replace_all`。空 `new` 删除。空 `old` 拒绝（新建走 write）。不按行号、不是正则。Codex 的 `apply_patch` 文法不学。

**先读再改**（学 Claude 的硬门，不学「必须读完全文」）：

- `ToolRuntime` 内存表：`绝对路径 → {content, mtime}`。不写文件系统。
- 成功 `read`（整份或 offset 翻页都算）记下快照。pdf/docx/xlsx 抽取 **不记账**。
- `edit`、覆盖**已有文件**的 `write`：从没 read、或磁盘相对上次读 **mtime 且内容都变了** → 失败并写明原因。只变 mtime、内容相同则放行并刷新 timestamp。
- **新建** write 不需要先 read；写成功后记账，紧接着 edit 不用再读。
- edit/write 成功立刻用新内容和新 mtime 覆盖记录，同一轮可以连改。

read 分页是「全文进内存再切一页给模型」。默认也可能只返回前 1000 行。这 **不影响**「有没有 read 过」：有成功 read 就能 edit；`old` 仍在磁盘全文里匹配。不要用「是否传了 offset」当门禁，否则大文件第一次默认 read 也不算数，上下文会被撑爆。

成功 edit 后：`edit_diff` 事件给 TUI 画红绿 hunk（上下 2 行上下文，最多约 14 行）。tool 结果里带同一份纯文本补丁给模型。`replace_all` 只展示第一处 + `… and N more`。不上备份、LSP、弯引号。

---

## 13. TUI

`cli/tui.py` 订 `events`，不改 jsonl。`uv run perm` 开 TUI；`-s <id> "<问题>"` 仍是一次性 CLI。须用项目 `.venv`。界面名叫 Permanent。Python logging 和 browser-use MCP 的 stderr 写在当前会话目录：`chats/{id}/permanent.log`、`chats/{id}/browser-use-mcp.log`（TUI 不打到终端）。 `/new` `/resume` 会换到新会话的日志文件。空会话居中一张浅框：左边永恒号（环状模块舱），右边 `/new` `/resume` `/help`。`/new` 开空白会话；`/resume` 列出本项目会话（每页 10 条，点行或 ↑↓ 加回车进入，←→ 翻页），也可用 `/resume 1` 或 `/resume <id>`。标题在 `chats/{id}/title.json`（目录名仍是 id）：首条用户话自动写入，`/rename` 记 `title_is_manual`。粘贴/拖入绝对图片路径或剪贴板位图变成 `[Image #N]`，文件落在 `chats/{id}/input/images/`；user entry 记 `media.path`，投影时挂在这条 user 上。散文里的路径不当图。时间线滚动立刻跳（关掉 Textual 默认惯性动画），滚轮一次 4 行。

过程行：灰色菱形 + 加粗动词。工具还在跑时，这一行的菱形在实心和空心之间闪，右边是已经用了的秒数；跑完菱形停住，不足 1 秒不留秒数。流式思维链在时间线实时走 `Thinking… Xs`，该段结束落下 `Thought for Xs`，不展开思考原文。没有正文、而且不到 1 秒的 Thought 不留下。下一段思考另起一行。回合结束只在时间线落一行：

`Worked for 1m41s | deepseek-v4-flash | 112K / 1M`

压过窗口再接 `| compacted …`。`memory flush no_reply` 不进时间线。

正文是 Static + Rich 画成可选中文本；拖选后 Ctrl+C。标题后空一行，列表项不加倍行距。加粗、标题、表头是黑体；蓝色只留给链接、文件名（行内代码）。markdown 表格用 Unicode 方框。工具/Thought 行高 1。markdown 引用块灰色，不要品红。

---

## 14. 还没做的

- skill 斜杠命令（`/browser-use` 强制注入）；现在只有名单 + read SKILL.md  
- Jev 三处（工具放行 / fetch 过筛 / Flush 门），见 `docs/jev.md`  
- TUI 闲时定时 Flush/Dream（现在是每轮停稳 Flush，过门才 Dream）  
- bash 边跑边往文件流（现在仍先整段进内存再量）  
- schema 从 registry 生成（现在两份清单）  
- 同一 `reply()` 跑着时另一路 HTTP 自动入队（现在请显式 `inbox.push_steer` / `push_follow_up`）  
- 多 lane、SQLite  

---

## 15. 下一步

循环、工单、压缩、恢复可以停。建议：

1. skill 正文写实（browser-use CLI）；斜杠命令可选  
2. schema 与 registry 合成一份  
3. 需要时再做 bash 流式落盘  

---

## 文件对照

```text
prompt/assemble.py      拼 system（yaml + tool_notes + skills + AGENTS.md + cwd + 日期）
prompt/skills.py        扫描 SKILL.md，生成短名单
prompt/system_prompt.yaml  人设 + 按工具启用的注意事项
loop.py                 两层循环 + 压缩检查点
paths.py                ~/.permanent 布局
memory/                 Flush / Dream / 召回
inbox.py / abort.py / compaction.py / recover.py / session_log.py / envfile.py
runtime/                工单、并行、hook、先读再改快照（Jev 客户端以后放这里）
tools/                  模型能调的工具 + registry + schemas
docs/jev.md             工具放行 / fetch 过筛 / Flush 门
llm/deepseek.py
cli/tui.py              Grok 风格时间线
cli/app.py              入口
cli/trace.py            终端过程日志 + events
events.py               内核 → 界面
docs/harness.md         本文（含跨会话 memory）
```

独立入口：`uv run perm` 打开 TUI；`uv run perm -s <id> "<问题>"` 仍是一次性 CLI。
