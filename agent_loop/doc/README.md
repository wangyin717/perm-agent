# Agent Loop

这套 loop 参考 pi harness-v2：本地可跑、jsonl 可对账。  
还不是完整 pi（没有并行 tool、compaction、多 lane、SQLite）。

加工具：写 `execute` + `REPLAY` + 自己的 hooks，登记进 `TOOLS`，不必改循环。

---

## 1. React loop

入口：`ReactAgentLoop.reply` → `_run_loop` → `_run_outer_inner`。

```text
读 jsonl，apply_recovery（先给上一世收尸）
再 inspect 一次
continue_llm → 本轮新话进 inbox.steer（插队）
其它该写 user → append user
messages = [system] + jsonl 的 entry 投影

外层 while:
  内层 while (还有 tool 或 steer):
    drain steer → 写 user entry + 推进 messages
    写 step_attempt（或复用未关的 assistant id）
    llm.call
    end_turn / 无 tool_calls → 写 assistant，内层可停
    否则跑工具，tool result 进 messages
  内层停稳 → drain follow_up，变成 steer，再开一圈内层
  没有 follow_up → return
```

LLM 每步只有两种出口：`end_turn` 或 `tool_use`。没有 `MAX_STEPS`，和 pi 一样靠模型停；abort / 工具 `terminate` 可提前停。

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
- bash：`before_tool_deny_rm`、`after_tool_truncate_output`

加工具：`NAME` / `REPLAY` / `BEFORE_HOOKS` / `AFTER_HOOKS` / `execute` → `tools/registry.py`。

---

## 4. Entry 和 Record

`<workspace>/.agent/sessions/<session_id>.jsonl`，一行一次提交。

| kind | type | 是什么 | 进模型吗 |
|---|---|---|---|
| record | step_attempt | 即将打 LLM，预分配 assistant id | 否 |
| record | tool_started | 即将执行工具 | 否 |
| entry | user / assistant / tool_result | 对话 | 是 |

- `step_attempt.result_entry_id` = 随后那条 assistant 的 `id`
- `tool_started.result_id` = 随后那条 tool_result 的 `result_id`
- blocked / unknown 没有 started，result 自己生成 id

权威是 jsonl。内存 list 只是本轮缓存。

样例：`doc/sample_session_with_step_attempt.jsonl`

---

## 5. 进程崩溃恢复

下次 `_run_loop` **开头**读 jsonl，不猜进程。当前进程里的异常走 `try/except`，不走 `recover.py`。

| 磁盘 | action | 现在会不会自动做 |
|---|---|---|
| started 无同 id 的 result | close_unfinished | **会**：两边 `replay==safe` 则重放；否则 `interrupted` |
| 最后是 assistant+tool_calls，还没 started | resume_tools | **会**：完整 `call_tool` |
| 有 step_attempt、没有同 id 的 assistant | （复用 id 再 `llm.call`） | **会**：`_begin_llm_step` 不 new id |
| 最后是 tool_result | continue_llm | **会**进 loop 再打模型；新 user 走 **steer** |
| 最后只有 user | start_llm | **会**投影后打模型；同一句 user 不重复写 |
| 最后是纯文本 assistant | idle | 无需恢复；新问题当本轮 user |

`replay` 要 **当时 jsonl 里的** 和 **现在代码里的 `REPLAY`** 都是 `safe` 才重放。bash 目前 `safe`。

Ctrl+C 是取消，不是崩溃：未关工具更合理写 interrupted，不要当 safe 重放。崩溃才是进程没了、只剩文件。

---

## 6. LLM 失败：重试 vs step_attempt（不要混）

三件不同的「再试一次」：

| | 发生在哪 | jsonl |
|---|---|---|
| 超时/429/5xx 重试 | **`DeepSeekLLM.call` 内部** | 仍是同一张 `step_attempt` |
| 欠费 402 / 401 / 400 / 文案带 quota | 立刻抛，不重试 | 有 attempt、没 assistant |
| 进程死在 `llm.call` | 下次 `_begin_llm_step` 复用 id | 有 attempt、没 assistant |

`call()` 最多 3 次（第 1 次 + 再试 2 次）。退避约 0.5s → 1s → 2s（封顶 8s）；有 `Retry-After` 听它，超过 60s 直接失败。429 文案若像欠费（quota/billing）**不重试**。

Ctrl+C：`Abort` 打断退避和卡住的 POST（`RetryCancelledError`）。不新写 attempt。

---

## 7. 每个工具的错误捕获（bash）

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

## 8. `wait_for` 杀不掉线程里的 `run`（讨论）

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

## 9. 还没做的

- `wait_for` 超时后打断沙箱里那次命令（见第 8 节）  
- 并行 tool、compaction、多 lane、SQLite  
- 同一 `reply()` 跑着时另一路 HTTP 自动入队（现在请显式 `inbox.push_steer` / `push_follow_up`）

---

## 10. 和 SA 解耦（讨论）

内核（`_run_loop` / `ToolRuntime` / jsonl / recover / inbox / llm / bash）几乎只依赖标准库 + `aiohttp` + 「有 `commands.run` 的 sandbox」。

绑在 SA 上的是外壳 `reply()`：

- `ExecutionDependencies`（Nacos/Session 那条链）
- `memory.conversation.rounds[-1].user_query`
- `event_emitter` + `Post` + `SendOutput` SSE
- `user_action_data` 大袋子（sessionId / workspace / e2b_sandbox）

独立开源时建议切成：**Kernel**（`run(user_query, *, sandbox, workspace, session_id) -> str`）+ **SA Adapter**（现有 `reply()` 只做翻译）。SA 继续用 Adapter；CLI/其它项目只依赖 Kernel。

---

## 11. 下一步（讨论）

循环已经够用。优先 **加工具**，不改 loop：每个工具 `NAME` / `REPLAY` / hooks / `execute`，登记 `TOOLS`。

建议顺序（对齐 pi / Grok Build 的 coding 底座）：

1. **read** — 读文件（offset/limit），`REPLAY=safe`
2. **write** — 整文件写，`REPLAY=never`
3. **edit** — 改一段（old/new 唯一匹配），`REPLAY=never`
4. **grep** — 仓库内搜内容，`REPLAY=safe`（比 bash rg 好截断、好对账）

有 **bash + 这四件** 就够当最小 coding agent。`list_dir` 可随后加（bash ls 能凑合）。todo / subagent / web / 图视频都后放。

---

## 文件对照

```text
agent_loop.py      reply + _run_loop + 两层循环
inbox.py           steer 插队 / follow_up 排队
abort.py           Ctrl+C → 打断 LLM 退避
tool_runtime.py    call_tool / started / result
session_log.py     jsonl
recover.py         inspect_log + apply_recovery + 投影 messages
tools/bash_tool.py 执行、REPLAY、hooks、超时重试
tools/records.py   StepAttemptRecord / ToolStartedRecord / ToolResultEntry
tools/registry.py  TOOLS
tools/hooks.py     before/after 分发
llm/deepseek.py    调用 + 可重试分类 + 退避
```
