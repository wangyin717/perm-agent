"""压缩：回合结束 80% 进场、中途仅超窗、micro + summary、最近 10% 完整轮次保留。"""

from __future__ import annotations

import asyncio

from agent_loop.loop import ReactAgentLoop
from agent_loop.compaction import (
    KEEP_TAIL_RATIO,
    OVERFLOW_RATIO,
    SUCCESS_RATIO,
    TRIGGER_RATIO,
    ContextUsageTracker,
    _keep_tail_start,
    _round_starts,
    _tail_start_index,
    estimate_tokens,
    maybe_compact,
    run_microcompact,
    run_summary_compaction,
)
from agent_loop.llm.deepseek import LLMResponse
from agent_loop.recover import entries_to_messages
from agent_loop.session_log import SessionLog, session_log_path
from agent_loop.runtime.records import ToolResultEntry


class ScriptedSummaryLLM:
    """摘要请求打桩：记录每次收到的 messages，回固定摘要文本。"""

    def __init__(self, summaries):
        self.summaries = list(summaries)
        self.calls = []

    async def call(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        text = self.summaries.pop(0)
        return _FakeResponse(text)


class _FakeResponse:
    def __init__(self, text):
        self.text = text
        self.tool_calls = []
        self.stop_reason = "end_turn"
        self.prompt_tokens = 0
        self.completion_tokens = 0


def _log(tmp_path):
    return SessionLog(session_log_path({"sessionId": "s1", "workspace": str(tmp_path)}))


def _seed_started(log, tool_call_id, tool_name, result_id, args=None):
    log.append_record(
        "tool_started",
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        effective_args=args or {},
        replay="safe" if tool_name in ("bash", "read", "grep") else "never",
        result_id=result_id,
    )


def _seed_round(log, idx, tool_name="bash", content_len=1000, is_error=False):
    """一整轮：user -> assistant+tool_call -> tool_result -> assistant(end_turn)。"""
    log.append_entry("user", content=f"round {idx} question")
    call_id = f"call-{idx}"
    result_id = f"result-{idx}"
    log.append_entry(
        "assistant",
        id=f"asst-{idx}-1",
        content="",
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": "{}"},
            }
        ],
    )
    _seed_started(log, call_id, tool_name, result_id)
    log.append_entry(
        "tool_result",
        result_id=result_id,
        tool_call_id=call_id,
        tool_name=tool_name,
        content="x" * content_len,
        is_error=is_error,
        terminate=False,
    )
    log.append_entry("assistant", id=f"asst-{idx}-2", content=f"round {idx} done")


def _over_trigger_log(log, rounds=10, content_len=2000, window=4000):
    """造一份 ratio ≥ 80% 的会话，返回 (messages, tracker)。"""
    for i in range(rounds):
        _seed_round(log, i, tool_name="bash", content_len=content_len)
    messages = [{"role": "system", "content": "sys"}]
    messages.extend(entries_to_messages(log.read_all()))
    tracker = ContextUsageTracker(context_window=window)
    tracker.bootstrap(messages)
    return messages, tracker


# ---------------------------------------------------------------------------
# 阈值常量
# ---------------------------------------------------------------------------


def test_threshold_constants():
    assert TRIGGER_RATIO == 0.60
    assert OVERFLOW_RATIO == 1.00
    assert SUCCESS_RATIO == 0.30
    assert KEEP_TAIL_RATIO == 0.10


# ---------------------------------------------------------------------------
# round boundary
# ---------------------------------------------------------------------------


def test_round_starts_splits_on_user_entries():
    entries = [
        {"type": "user"},
        {"type": "assistant"},
        {"type": "tool_result"},
        {"type": "assistant"},
        {"type": "user"},
        {"type": "assistant"},
    ]
    assert _round_starts(entries) == [0, 4]


def test_tail_start_index_never_splits_mid_round():
    entries = [
        {"type": "user", "content": "a" * 40},
        {"type": "assistant", "content": "b" * 40, "tool_calls": []},
        {
            "type": "tool_result",
            "result_id": "r1",
            "tool_name": "bash",
            "content": "c" * 400,
        },
        {"type": "assistant", "content": "d" * 40},
        {"type": "user", "content": "e" * 40},
        {"type": "assistant", "content": "f" * 40},
    ]
    round_starts = _round_starts(entries)
    # 预算只够最后一条，但切点必须回退到它所在轮次的起点（index 4），不能停在 index 5
    idx = _tail_start_index(entries, round_starts, tail_budget_tokens=1)
    assert idx == 4


def test_tail_start_index_zero_budget_keeps_nothing():
    entries = [{"type": "user", "content": "a"}]
    assert _tail_start_index(entries, [0], tail_budget_tokens=0) == len(entries)


def test_keep_tail_start_at_least_last_round(tmp_path):
    log = _log(tmp_path)
    for i in range(4):
        _seed_round(log, i, tool_name="bash", content_len=200)
    from agent_loop.recover import visible_entries

    entries = visible_entries(log.read_all(), 0)
    # 窗口很小，10% 预算盖不住最后一轮，切点仍应是最后一轮的 user
    idx = _keep_tail_start(entries, context_window=50)
    assert idx == _round_starts(entries)[-1]


# ---------------------------------------------------------------------------
# microcompact
# ---------------------------------------------------------------------------


def test_microcompact_redacts_old_safe_tool_result_outside_tail(tmp_path):
    log = _log(tmp_path)
    for i in range(6):
        _seed_round(log, i, tool_name="bash", content_len=1000)

    # context_window 小，10% 尾巴窄，前面几轮应该会被清理
    cleared = run_microcompact(log, log.read_all(), context_window=2000)
    assert cleared > 0

    rows = log.read_all()
    redacted = [r for r in rows if r.get("type") == "tool_result_redacted"]
    assert redacted
    for r in redacted:
        assert "redacted" in r["content"]
        assert "bash" in r["content"]

    messages = entries_to_messages(rows)
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert "redacted" in tool_msgs[0]["content"]
    # 最近一轮（在 keep 尾巴内）应该还是原始长内容
    assert tool_msgs[-1]["content"] == "x" * 1000


def test_microcompact_skips_never_replay_tools(tmp_path):
    log = _log(tmp_path)
    for i in range(6):
        _seed_round(log, i, tool_name="write", content_len=1000)

    cleared = run_microcompact(log, log.read_all(), context_window=2000)
    assert cleared == 0
    assert not [r for r in log.read_all() if r.get("type") == "tool_result_redacted"]


def test_microcompact_skips_short_results(tmp_path):
    log = _log(tmp_path)
    for i in range(6):
        _seed_round(log, i, tool_name="bash", content_len=100)

    cleared = run_microcompact(log, log.read_all(), context_window=2000)
    assert cleared == 0


def test_microcompact_no_context_window_is_noop(tmp_path):
    log = _log(tmp_path)
    _seed_round(log, 0, tool_name="bash", content_len=2000)
    assert run_microcompact(log, log.read_all(), context_window=0) == 0


def test_microcompact_is_idempotent_second_pass_finds_nothing_new(tmp_path):
    log = _log(tmp_path)
    for i in range(6):
        _seed_round(log, i, tool_name="bash", content_len=1000)

    first = run_microcompact(log, log.read_all(), context_window=2000)
    assert first > 0
    second = run_microcompact(log, log.read_all(), context_window=2000)
    assert second == 0


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_keeps_recent_tail(tmp_path):
    log = _log(tmp_path)
    for i in range(8):
        _seed_round(log, i, tool_name="bash", content_len=200)

    llm = ScriptedSummaryLLM(["## Goal\nfirst summary"])
    changed = asyncio.run(
        run_summary_compaction(log, log.read_all(), llm, context_window=2000)
    )
    assert changed is True

    rows = log.read_all()
    summary_rows = [r for r in rows if r.get("type") == "compaction_summary"]
    assert len(summary_rows) == 1
    assert summary_rows[0]["level"] == "summary"

    messages = entries_to_messages(rows)
    assert messages[0]["role"] == "user"
    assert "compacted" in messages[0]["content"]
    assert "first summary" in messages[0]["content"]
    tail_user_msgs = [m["content"] for m in messages[1:] if m["role"] == "user"]
    assert any(c.startswith("round ") for c in tail_user_msgs)
    assert tail_user_msgs[-1] == "round 7 question"


def test_cumulative_summary_passes_previous_summary_to_prompt(tmp_path):
    log = _log(tmp_path)
    for i in range(8):
        _seed_round(log, i, tool_name="bash", content_len=200)

    llm = ScriptedSummaryLLM(["## Goal\nfirst summary"])
    asyncio.run(run_summary_compaction(log, log.read_all(), llm, context_window=2000))

    for i in range(8, 12):
        _seed_round(log, i, tool_name="bash", content_len=200)

    llm2 = ScriptedSummaryLLM(["## Goal\nmerged summary"])
    changed = asyncio.run(
        run_summary_compaction(log, log.read_all(), llm2, context_window=2000)
    )
    assert changed is True
    prompt = llm2.calls[0]["messages"][1]["content"]
    assert "first summary" in prompt

    rows = log.read_all()
    summary_rows = [r for r in rows if r.get("type") == "compaction_summary"]
    assert len(summary_rows) == 2
    messages = entries_to_messages(rows)
    assert "merged summary" in messages[0]["content"]
    assert "first summary" not in messages[0]["content"]


def test_summary_compaction_noop_when_nothing_to_summarize(tmp_path):
    log = _log(tmp_path)
    _seed_round(log, 0, tool_name="bash", content_len=200)

    llm = ScriptedSummaryLLM(["should not be used"])
    changed = asyncio.run(
        run_summary_compaction(log, log.read_all(), llm, context_window=2000)
    )
    # 只有一轮，keep 切点就是这一轮本身，没有可摘要内容
    assert changed is False
    assert llm.calls == []


def test_summary_uses_micro_redactions_in_transcript(tmp_path):
    log = _log(tmp_path)
    for i in range(6):
        _seed_round(log, i, tool_name="bash", content_len=1000)
    run_microcompact(log, log.read_all(), context_window=2000)

    llm = ScriptedSummaryLLM(["## Goal\nafter micro"])
    asyncio.run(run_summary_compaction(log, log.read_all(), llm, context_window=2000))
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "redacted" in prompt
    assert "x" * 1000 not in prompt


# ---------------------------------------------------------------------------
# maybe_compact 接入点
# ---------------------------------------------------------------------------


def test_maybe_compact_noop_below_trigger(tmp_path):
    log = _log(tmp_path)
    _seed_round(log, 0, tool_name="bash", content_len=100)
    messages = [{"role": "system", "content": "sys"}]
    messages.extend(entries_to_messages(log.read_all()))
    tracker = ContextUsageTracker(context_window=1_000_000)
    tracker.bootstrap(messages)
    assert tracker.usage_ratio() < TRIGGER_RATIO

    llm = ScriptedSummaryLLM([])
    changed = asyncio.run(maybe_compact(log, messages, tracker, llm))
    assert changed is False
    assert llm.calls == []
    assert messages[0]["role"] == "system"


def test_maybe_compact_overflow_only_skips_when_under_window(tmp_path):
    """回合中途：已经过了 80% 但还没满窗，不该拆当前回合的前缀。"""
    log = _log(tmp_path)
    messages, tracker = _over_trigger_log(log)
    tracker.known_tokens = int(tracker.context_window * 0.85)
    assert TRIGGER_RATIO <= tracker.usage_ratio() < OVERFLOW_RATIO
    llm = ScriptedSummaryLLM(["## Goal\nshould not run"])
    changed = asyncio.run(
        maybe_compact(log, messages, tracker, llm, overflow_only=True)
    )
    assert changed is False
    assert llm.calls == []


def test_maybe_compact_overflow_only_runs_when_window_full(tmp_path):
    log = _log(tmp_path)
    messages, tracker = _over_trigger_log(log)
    tracker.known_tokens = tracker.context_window
    llm = ScriptedSummaryLLM(["## Goal\noverflow summary"])
    changed = asyncio.run(
        maybe_compact(log, messages, tracker, llm, overflow_only=True)
    )
    assert changed is True
    assert llm.calls


def test_maybe_compact_cascade_micro_then_summary(tmp_path):
    log = _log(tmp_path)
    messages, tracker = _over_trigger_log(log)
    assert tracker.usage_ratio() >= TRIGGER_RATIO

    llm = ScriptedSummaryLLM(["## Goal\ncascade summary"])
    changed = asyncio.run(maybe_compact(log, messages, tracker, llm))
    assert changed is True
    assert any(r.get("type") == "tool_result_redacted" for r in log.read_all())
    assert llm.calls  # micro 之后仍 >30%，同一集里接着 summary
    assert messages[0]["role"] == "system"
    assert any(
        "cascade summary" in (m.get("content") or "") for m in messages if m.get("role") == "user"
    )


def test_maybe_compact_preserves_system_prompt(tmp_path):
    log = _log(tmp_path)
    messages, tracker = _over_trigger_log(log)
    llm = ScriptedSummaryLLM(["## Goal\nkeep system"])
    asyncio.run(maybe_compact(log, messages, tracker, llm))
    assert messages[0] == {"role": "system", "content": "sys"}


def test_maybe_compact_rebuilds_system_when_given(tmp_path):
    log = _log(tmp_path)
    messages, tracker = _over_trigger_log(log)
    llm = ScriptedSummaryLLM(["## Goal\nkeep system"])
    asyncio.run(
        maybe_compact(
            log, messages, tracker, llm, system_content="assembled sys"
        )
    )
    assert messages[0] == {"role": "system", "content": "assembled sys"}


def test_maybe_compact_empty_summary_keeps_micro_progress(tmp_path):
    log = _log(tmp_path)
    messages, tracker = _over_trigger_log(log)
    llm = ScriptedSummaryLLM([""])  # 摘要失败
    changed = asyncio.run(maybe_compact(log, messages, tracker, llm))
    assert any(r.get("type") == "tool_result_redacted" for r in log.read_all())
    assert changed is True  # micro 已经落盘
    assert not any(
        r.get("type") == "compaction_summary" for r in log.read_all()
    )


# ---------------------------------------------------------------------------
# tracker 记账：completion 已经是 assistant，tool 路径不能再估一遍
# ---------------------------------------------------------------------------


def test_handle_tool_calls_does_not_double_count_assistant(tmp_path):
    """update_from_response 已经把 prompt+completion 写入 known_tokens。

    completion 就是即将 append 的 assistant 消息，再 add_estimate(assistant)
    会让下一枪 compact 判断虚高。只该补上 completion 里没有的 tool_result。
    """
    loop = ReactAgentLoop(None, None, None)
    loop._log = _log(tmp_path)
    result = ToolResultEntry(
        result_id="r1",
        tool_call_id="call-1",
        tool_name="read",
        content="hello world",
    )

    class _StubRuntime:
        async def _prepare_call(self, tool_call):
            from agent_loop.runtime.tool_runtime import PreparedCall

            return PreparedCall(
                tool_call=tool_call,
                call_id=tool_call.get("id") or "",
                name="read",
                result=result,
            )

        async def _execute_prepared(self, prepared, sandbox=None):
            return result

    loop.runtime = _StubRuntime()
    response = LLMResponse(
        text="reading",
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }
        ],
        stop_reason="tool_use",
        prompt_tokens=10_000,
        completion_tokens=400,
    )
    tracker = ContextUsageTracker(context_window=1_000_000)
    tracker.update_from_response(response)

    asyncio.run(loop._handle_tool_calls([], response, "asst-1", tracker))

    assert tracker.known_tokens == 10_000 + 400 + estimate_tokens(result.to_message())
