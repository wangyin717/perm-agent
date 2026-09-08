# Agent Loop

这套 loop 参考 pi harness-v2：本地可跑、jsonl 可对账。  
还不是完整 pi（没有多 lane、SQLite）。

加工具：写 `execute` + `REPLAY` + `READ_ONLY` + 自己的 hooks，登记进 `TOOLS`，不必改循环。

---

## 1. React loop

入口：`ReactAgentLoop.reply` → `_run_loop` → `_run_outer_inner`。

```text
读 jsonl，apply_recovery（先给上一世收尸）
再 inspect 一次
continue_llm → 本轮新话进 inbox.steer（插队）
其它该写 user → append user
messages = [system] + jsonl 的 entry 投影（已应用 redaction / summary）

外层 while:
  内层 while (还有 tool 或 steer):
    若上一枪已 end_turn、接下来要吃 steer → 按 80% 压（新回合）
    drain steer → 写 user entry + 推进 messages
    写 step_attempt（或复用未关的 assistant id）
    仅当下一枪估算 ≥100% 窗口 → 压（回合中途保命）
    llm.call
    end_turn / 无 tool_calls → 写 assistant，内层可停
    否则跑工具（准备串行、执行按路径锁并发），tool result 进 messages
  内层停稳 = 回合结束 → 按 80% 压
  drain follow_up，变成 steer，再开一圈内层
  没有 follow_up → return
```

LLM 每步只有两种出口：`end_turn` 或 `tool_use`。没有 `MAX_STEPS`，和 pi 一样靠模型停；abort / 工具 `terminate` 可提前停（`terminate` 只截断喂给模型的投影，jsonl 仍记下所有已执行的结果）。

`reply` 必须 `post_proxy.end()` 且 `send_to=User`，PostScheduler 才结束本轮。

---

## 2. 内层插队 / 外层排队

`inbox.py`：`UserInbox`

| | 方法 | 何时进模型 |
|---|---|---|
| **插队 steer** | `loop.inbox.push_steer(text)` | 当前 step 结束之后（tool result 已进 messages）、下一次 `llm.call` 之前 |
| **排队 follow_up** | `loop.inbox.push_follow_up(text)` | 内层认为可以停了（没 tool、也没待插队）再进 |

不会在 `execute` 半截插入。崩溃恢复后若是 `continue_llm`（差最终回复），这次 HTTP 的新 user **走插队**，不再丢掉（例如「用英文回复」下一枪 LLM 能看见）。

「请继续」更像 follow_up；「请继续，用英文」更像 steer。

end_turn 之后才进的 steer 当成**新回合**：先按 80% 做压缩，再注入。

---

## 3. 工具 hook

按工具名分发。

```text
查表 get_tool
  → before_tool     可改 args，或 block
  → tool_started    盖章：effective_args + result_id + replay
  → execute         必须用 started.effective_args
  → after_tool      可改 content / is_error / terminate
  → tool_result     同一 result_id 关单
```

- hook 只返回意见，**不准** `messages.append`
- `block` / 未知工具：**不写** started，直接 `create_error_tool_result`
- bash / read / grep：`after_tool_truncate_output`（8k 字符）。这是写入时截断，第一次进 messages 就是短的，不改已经 cache 过的前缀。不要靠事后 micro 替代这道闸——micro 碰不到最近 10% 尾巴，而最大的工具结果往往就在那里。
- bash：另有 `before_tool_deny_rm`

加工具：`NAME` / `REPLAY` / `READ_ONLY` / `BEFORE_HOOKS` / `AFTER_HOOKS` / `execute` → `tools/registry.py`。

---

## 3.1 工具并行

一批 `tool_calls` 分两阶段（`tool_concurrency.py`）：

```text
阶段一（严格串行）  逐个 before_tool + 写 tool_started
                   unknown / blocked 直接产出错误结果，不进阶段二
阶段二（并发）      READ_ONLY=False 且有 path 的调用，按解析后的绝对路径建锁
                   任何调用命中这些写路径就排队；其余（含 bash）直接并发
                   asyncio.gather 等完，返回顺序 = 原始调用顺序
```

| 工具 | `READ_ONLY` | 锁 |
|---|---|---|
| read / grep | True | 有 `path` 才参与；撞上本批某条写路径才排队 |
| write / edit | False | 按解析后的绝对路径字符串全等加锁（不做目录包含） |
| bash | 无 path | 不加锁 |

`terminate`：四个都跑完。jsonl 照实记录所有 `tool_result`。喂给模型的 messages / `entries_to_messages` 投影按 `tool_calls` 顺序排，截到第一条 `terminate=True`，并把该条 assistant 的 `tool_calls` 裁到与保留的 result 对齐。恢复仍只看工单关没关，不认 terminate。

---

## 4. Entry 和 Record

`<workspace>/.agent/sessions/<session_id>.jsonl`，一行一次提交。

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

权威是 jsonl。内存 list 只是本轮缓存。

样例：`doc/sample_session_with_step_attempt.jsonl`

---

## 5. 上下文压缩

`compaction.py`。为了 KV / prompt cache：能追加就追加到回合结束，压缩是回合之间的换班，不是每枪手术。

### 检查时机

| 时机 | 条件 | 在哪 |
|---|---|---|
| 回合结束 | `ratio ≥ 80%` | 内层停稳之后；或 end_turn 后改吃 steer 之前 |
| 回合中途 | `ratio ≥ 100%`（下一枪会超窗） | 每次 `llm.call` 之前，`overflow_only=True` |

低于 80% 不改已经发出去的前缀。中途 80%–100% 之间继续追加，让当前回合吃热 cache。

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
| `TRIGGER_RATIO` | 80% | 回合结束进场 |
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

`replay` 要 **当时 jsonl 里的** 和 **现在代码里的 `REPLAY`** 都是 `safe` 才重放。bash / read / grep 目前 `safe`；write / edit 是 `never`。

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

---

## 8. 每个工具的错误捕获（bash）

失败必须 **throw**，`ToolRuntime` 统一 `create_error_tool_result`（`is_error=true`），loop 继续，模型能看见。

1. 没沙箱  
2. 没命令  
3. 执行错误：`run` throw，或 `exit_code != 0`  
4. 超时（两层，只重试超时、且只 1 次）

| 层 | 是什么 |
|---|---|
| 内层 | E2B `commands.run(..., timeout=)` |
| 外层 | `asyncio.wait_for`，防止 SDK 卡住 |

第一次超时 → 同一 cmd 再跑（不进 LLM）。第二次还超时 → throw → 失败 result → LLM 决定。  
非 0 / 没沙箱 / 空 cmd **不重试**。

`wait_for` **杀不掉**线程里可能还在跑的 `run`（只放弃等待）。含义见下节。

活着失败：当场关单。只有进程死在 execute 里，才留给 `inspect_log`。

`asyncio.to_thread`：因为 `commands.run` 是同步阻塞的。

---

## 9. `wait_for` 杀不掉线程里的 `run`（讨论）

代码是：

```text
asyncio.wait_for(
    asyncio.to_thread(sandbox.commands.run, cmd, timeout=...),
    timeout=...,
)
```

两层东西：

- **`to_thread`**：在**线程池的一条线程**里同步调用 E2B `run`（RPC 阻塞到沙箱回包）
- **`wait_for`**：只取消**正在 await 的协程**（「我不等了」）

超时后发生的是：协程抛 `TimeoutError`，`ToolRuntime` 关单，loop 继续。  
**那条线程还堵在 `commands.run` 里**，直到 SDK 自己返回或进程退出。Python 没有安全的 `thread.kill()`。

所以「杀不掉」= **放弃等待 ≠ 打断远端那次 RPC / 沙箱里的进程。**

可能的后果：超时后立刻重试，旧 `run` 和新 `run` 在沙箱里叠着；线程池多占一条。

真要停，只能打**沙箱侧**（`kill` 进程、`sandbox.kill()` 整箱），不是打 Python 线程。这还没做。

---

独立项目入口：`python -m agent_loop "<问题>"`（`agent_loop/__main__.py`）。bash 默认本机执行，不再依赖 E2B。SA 的 SSE 气泡已去掉。

---

## 10. 还没做的

- `wait_for` 超时后打断沙箱里那次命令（见第 9 节）  
- 多 lane、SQLite  
- 同一 `reply()` 跑着时另一路 HTTP 自动入队（现在请显式 `inbox.push_steer` / `push_follow_up`）

---

## 12. 下一步（讨论）

循环已经够用。优先 **加工具**，不改 loop：每个工具 `NAME` / `REPLAY` / hooks / `execute`，登记 `TOOLS`。

建议顺序（对齐 pi / Grok Build 的 coding 底座）：

1. **read** — **已做。** `path` + 可选 `offset`/`limit`，行号输出，`REPLAY=safe`；相对路径拼 workspace，绝对路径原样；after 截断
2. **write** — **已做。** 整文件写（`path` + `content`），`REPLAY=never`；路径规则同 read；缺目录会 mkdir
3. **edit** — **已做。** 精确替换（`path` / `old` / `new`，可选 `replace_all`），`old` 必须唯一除非 `replace_all`；`REPLAY=never`；空 old 在 before_tool 拦住
4. **grep** — **已做。** 正则搜内容（`pattern`，可选 `path` / `glob`），`path:line:content`，最多 50 条并带总数 footer；`REPLAY=safe`；有 rg 用 rg，否则 Python 走目录

有 **bash + read + write + edit + grep** 就够当最小 coding agent。压缩已接进 loop。

---

## 文件对照

```text
agent_loop.py      reply + _run_loop + 两层循环 + 压缩检查点
inbox.py           steer 插队 / follow_up 排队
abort.py           Ctrl+C → 打断 LLM 退避
compaction.py      maybe_compact / microcompact / summary / tracker
tool_runtime.py    call_tool / started / result（prepare + execute 可拆）
tool_concurrency.py 一批 tool_calls：准备串行、按路径锁并发
session_log.py     jsonl
recover.py         inspect_log + apply_recovery + 投影 messages（含压缩）
tools/bash_tool.py 执行、REPLAY、hooks、超时重试
tools/read_tool.py / write_tool.py / edit_tool.py / grep_tool.py
tools/records.py   StepAttemptRecord / ToolStartedRecord / ToolResultEntry
tools/registry.py  TOOLS
tools/hooks.py     before/after 分发
llm/deepseek.py    调用 + 可重试分类 + 退避
```
