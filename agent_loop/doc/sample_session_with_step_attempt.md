# 带 step_attempt 的会话样例

完整文件：`sample_session_with_step_attempt.jsonl`（7 行，一轮「写代码并运行」成功跑完）。

和现在的差别只有：**每次 `llm.call` 之前多一行 record**，assistant 的 `id` 必须等于这行的 `result_entry_id`。

```text
1 E user
2 R step_attempt  result_entry_id=asst-1     ← 即将打第一次 LLM
3 E assistant     id=asst-1  + tool_calls     ← 用这个 id 关单
4 R tool_started  result_id=tool-1
5 E tool_result   result_id=tool-1
6 R step_attempt  result_entry_id=asst-2     ← 即将打第二次 LLM（看工具结果写总结）
7 E assistant     id=asst-2                  ← 最终回复
```

加在 loop 的两个节点（每次 `llm.call` 前后）：

```text
写 step_attempt（预分配 assistant id）
await llm.call(...)
写 assistant entry（id = 刚才那个 result_entry_id）
```

工具链不用改。

## 崩在哪一行

只看磁盘上「有 attempt、有没有同 id 的 assistant」。

| 文件停在 | 含义 | 恢复 |
|---|---|---|
| 只有 user | LLM 还没开始（连 attempt 都没写） | 投影 messages，再走第一次 `llm.call`（并先写 attempt） |
| 有 attempt `asst-1`，没有 id=`asst-1` 的 assistant | **第一次 LLM 中途挂了** | 再打模型，成功后仍用 `asst-1` 写 assistant（不要 new 一个 id） |
| 有 asst-1 + tool_calls，没有 tool_started | 和现在一样 X1/X2 | `resume_tools` |
| 有 tool_started，没有 tool-1 | 和现在一样 X3/X4 | replay / interrupted |
| 有 tool_result，没有 attempt `asst-2` | 工具关了，第二次 LLM 还没开始 | 投影后写 `asst-2` 的 attempt，再 `llm.call` |
| 有 attempt `asst-2`，没有 id=`asst-2` 的 assistant | **总结那次 LLM 中途挂了** | 再打模型，用 `asst-2` 关单 |
| 7 行都在 | idle | 不用恢复 |

没有 `step_attempt` 时，第 2 行和第 6 行不存在，停在「只有 user」或「只有 tool_result」只能猜该再问模型，不知道是「还没开始打」还是「打到一半死了」，也没有预分配的 assistant id。

`attempt: 1` 以后若要限重试，同一步可以再 append `attempt: 2`、同一个 `result_entry_id`；这是后话。样例里每步只尝试一次。
