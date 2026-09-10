# Agent Loop

这套 loop 参考 pi harness-v2：本地可跑、jsonl 可对账。  
还不是完整 pi（没有多 lane、SQLite、skill）。

加工具：写 `execute` + `REPLAY` + `READ_ONLY` + 自己的 hooks，登记进 `TOOLS`，不必改循环。

密钥：仓库根 `.env`（`DEEPSEEK_API_KEY`、`PERPLEXITY_API_KEY`），启动时 `envfile.load_dotenv()` 读入；已有环境变量不覆盖。`.env` 不进 git，`.env.example` 只有变量名。

蓝图（已完成 / 要工程化 / 没做）：`doc/blueprint.html`。

---

## 1. React loop

入口：CLI 走 `ReactAgentLoop._run_loop`。`reply(memory, …)` 还在，是 SwiftAgent 时期的外壳，本仓库不用。

```text
读 jsonl，apply_recovery（先给上一世收尸）
再 inspect 一次
continue_llm → 本轮新话进 inbox.steer（插队）
其它该写 user → append user
messages = [system yaml] + jsonl 的 entry 投影（已应用 redaction / summary）

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

人设 `prompt/system_prompt.yaml` 启动时读死，发出去前不会再拼别的块（skill 清单以后要接在这里，现在没有这步）。

---

## 2. 内层插队 / 外层排队

`inbox.py`：`UserInbox`

| | 方法 | 何时进模型 |
|---|---|---|
| **插队 steer** | `loop.inbox.push_steer(text)` | 当前 step 结束之后（tool result 已进 messages）、下一次 `llm.call` 之前 |
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
- 写入时截断（第一次进 messages 就是短的，不改已经 cache 过的前缀）：

  | 工具 | after hook |
  |---|---|
  | bash / read / grep | 8k 字符 |
  | web_search | 32k |
  | web_fetch | 64k（落盘后回给模型的是短预览，一般碰不到） |

- bash：另有 `before_tool_deny_rm`

加工具：`NAME` / `REPLAY` / `READ_ONLY` / `BEFORE_HOOKS` / `AFTER_HOOKS` / `execute` → `tools/registry.py`。

当前工具：`bash` / `read` / `write` / `edit` / `grep` / `web_search` / `web_fetch`。

---

## 3.1 工具并行

一批 `tool_calls` 分两阶段（`tool_concurrency.py`）：

```text
阶段一（严格串行）  逐个 before_tool + 写 tool_started
                   unknown / blocked 直接产出错误结果，不进阶段二
阶段二（并发）      READ_ONLY=False 且有 path 的调用，按解析后的绝对路径建锁
                   任何调用命中这些写路径就排队；其余直接并发
                   asyncio.gather 等完，返回顺序 = 原始调用顺序
```

| 工具 | `READ_ONLY` | 锁 |
|---|---|---|
| read / grep | True | 有 `path` 才参与；撞上本批某条写路径才排队 |
| write / edit | False | 按解析后的绝对路径字符串全等加锁（不做目录包含） |
| bash / web_search / web_fetch | 无仓库 path | 不加锁 |

`terminate`：一批都跑完。jsonl 照实记录所有 `tool_result`。喂给模型的 messages / `entries_to_messages` 投影按 `tool_calls` 顺序排，截到第一条 `terminate=True`，并把该条 assistant 的 `tool_calls` 裁到与保留的 result 对齐。恢复仍只看工单关没关，不认 terminate。

---

## 4. 会话目录

`<workspace>/.agent/sessions/<session_id>/session.jsonl`，一行一次提交。

草稿纸在同一会话目录下，**不进 jsonl**，不进模型投影：

```text
.agent/sessions/<id>/
├── session.jsonl
└── workspace/tools_result/
    ├── grep/<12位hex>        一次搜索的完整命中（cursor 翻页读它）
    └── web_fetch/<12位hex>   一页全文（模型 grep/read 这条路径）
```

`ToolRuntime.session_dir` = jsonl 的父目录（`attach_log` 时赋上）。grep / fetch 用它写草稿纸；`workspace` 仍是用户仓库根，搜代码用这个。

从仓库根 grep 会跳过名为 `.agent` 的目录。要搜 fetch 快照，必须 `path=` 指到那个文件（或 `tools_result/web_fetch`）。

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

活着失败：当场关单。只有进程死在 execute 里，才留给 `inspect_log`。

`asyncio.to_thread` + `wait_for`：本机 `subprocess.run(..., timeout=)` 超时会杀子进程；`wait_for` 仍杀不掉线程里卡住的调用。真要停远端沙箱里的命令，还没做（现在默认不走 E2B）。

---

## 9. 各工具要点

| 工具 | 要点 |
|---|---|
| read | `path` + 可选 `offset`/`limit`，`LINE\|content`；`REPLAY=safe` |
| write | 整文件；`REPLAY=never`；缺目录 mkdir |
| edit | 精确替换；空 old 在 before_tool 拦住；`REPLAY=never` |
| grep | 正则；第一页 20 条；完整命中写入 `tools_result/grep/<id>`；footer 带 cursor 则再调 grep 只传 cursor；无 `session_dir` 不写盘、不给 cursor |
| web_search | `query`，可选 `max_results`（默认 5，上限 10）。有 `PERPLEXITY_API_KEY` 走 Search API；否则（或 401/429/5xx/超时）走 DuckDuckGo 子进程。结果带 `[perplexity]` / `[duckduckgo]` / `[duckduckgo fallback]`。snippet 仍进 jsonl |
| web_fetch | 一次一个公开 https URL；拦内网；跳转每次再检查。全文写入 `tools_result/web_fetch/<id>`；tool_result 只留路径、字数、约 30 行预览。找页内文字用 grep/read 且 `path=` 该文件 |
| bash | 本机命令；8k 截断；不要用来读改搜文件或搜网/拉页 |

web_search 的 DuckDuckGo 在独立子进程里跑 `ddgs`，30 秒杀不掉就 SIGKILL，避免卡住 loop。

---

## 10. 还没做的

- skill（磁盘 SKILL.md；人设后面拼清单；不进核心工具表）  
- 跨会话 memory（不是 jsonl，也不是压缩摘要）  
- 拼这一枪 system 的单独步骤（现在是 loop 里两行胶水）  
- 插件 / 浏览器 / 桌面键鼠  
- bash 大输出落盘（现在只有 8k 截断）  
- 同一 `reply()` 跑着时另一路 HTTP 自动入队（现在请显式 `inbox.push_steer` / `push_follow_up`）  
- 多 lane、SQLite  

---

## 11. 下一步

循环和五件套可以停。建议：

1. 冻压缩记账格式；进场比例与文档保持同一数字（现在是 60%）  
2. 抽出「拼 system」：yaml + 以后的 skill 清单  
3. skill  
4. memory 先当仓库文件，不单独立项  

---

## 文件对照

```text
agent_loop.py         reply + _run_loop + 两层循环 + 压缩检查点
inbox.py              steer 插队 / follow_up 排队
abort.py              Ctrl+C → 打断 LLM 退避
compaction.py         maybe_compact / microcompact / summary / tracker
envfile.py            读 .env
tool_runtime.py       call_tool；workspace + session_dir
tool_concurrency.py   一批 tool_calls：准备串行、按路径锁并发
session_log.py        .agent/sessions/<id>/session.jsonl
recover.py            inspect_log + apply_recovery + 投影 messages
tools/bash_tool.py / read_tool.py / write_tool.py / edit_tool.py
tools/grep_tool.py    分页 + 快照
tools/web_search_tool.py / web_search_ddgs_worker.py
tools/web_fetch_tool.py  落盘 + 短预览
tools/records.py      StepAttemptRecord / ToolStartedRecord / ToolResultEntry
tools/registry.py     TOOLS
tools/hooks.py        before/after 分发
llm/deepseek.py       调用 + 可重试分类 + 退避
prompt/system_prompt.yaml
doc/blueprint.html    模块蓝图
```

独立入口：`python -m agent_loop -s <id> "<问题>"`。
