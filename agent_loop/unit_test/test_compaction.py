"""压缩三档：pick_level 边界、轮次切分、microcompact 清理、half/full 摘要投影。"""

from __future__ import annotations

import asyncio

from agent_loop.compaction import (
    ContextUsageTracker,
    _round_starts,
    _tail_start_index,
    maybe_compact,
    pick_level,
    run_microcompact,
    run_summary_compaction,
)
from agent_loop.recover import entries_to_messages
from agent_loop.session_log import SessionLog, session_log_path


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


# ---------------------------------------------------------------------------
# pick_level
# ---------------------------------------------------------------------------


def test_pick_level_below_threshold_returns_none():
    assert pick_level(0.10) is None


def test_pick_level_boundaries():
    assert pick_level(0.29) is None
    assert pick_level(0.30) == "microcompact"
    assert pick_level(0.60) == "microcompact"
    assert pick_level(0.79) == "microcompact"
    assert pick_level(0.80) == "half_summary"
    assert pick_level(0.89) == "half_summary"
    assert pick_level(0.90) == "full_compact"
    assert pick_level(0.99) == "full_compact"


def test_pick_level_only_highest_hit_wins():
    # 0.82 同时越过 0.30 和 0.80 两条线，只应命中 half_summary
    assert pick_level(0.82) == "half_summary"


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


# ---------------------------------------------------------------------------
# microcompact
# ---------------------------------------------------------------------------


def test_microcompact_redacts_old_safe_tool_result_outside_tail(tmp_path):
    log = _log(tmp_path)
    for i in range(6):
        _seed_round(log, i, tool_name="bash", content_len=1000)

    # context_window 小，tail 预算窄，前面几轮应该会被清理
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
    # 最早一轮的工具结果应该已经被占位符覆盖
    assert "redacted" in tool_msgs[0]["content"]
    # 最近一轮（在保护尾部内）应该还是原始长内容
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
# half_summary / full_compact
# ---------------------------------------------------------------------------


def test_full_compact_keeps_only_last_round(tmp_path):
    log = _log(tmp_path)
    for i in range(4):
        _seed_round(log, i, tool_name="bash", content_len=200)

    llm = ScriptedSummaryLLM(["## Goal\nsummarized rounds 0-2"])
    changed = asyncio.run(
        run_summary_compaction(log, log.read_all(), llm, "full_compact", context_window=2000)
    )
    assert changed is True

    rows = log.read_all()
    summary_rows = [r for r in rows if r.get("type") == "compaction_summary"]
    assert len(summary_rows) == 1
    assert summary_rows[0]["level"] == "full_compact"

    messages = entries_to_messages(rows)
    assert messages[0]["role"] == "user"
    assert "compacted" in messages[0]["content"]
    assert "summarized rounds 0-2" in messages[0]["content"]
    # 最后一轮完整保留：user + assistant(tool_call) + tool + assistant
    remaining = messages[1:]
    assert remaining[0]["content"] == "round 3 question"
    assert any(m.get("role") == "tool" for m in remaining)


def test_half_summary_keeps_recent_tail(tmp_path):
    log = _log(tmp_path)
    for i in range(8):
        _seed_round(log, i, tool_name="bash", content_len=200)

    llm = ScriptedSummaryLLM(["## Goal\nfirst summary"])
    changed = asyncio.run(
        run_summary_compaction(log, log.read_all(), llm, "half_summary", context_window=2000)
    )
    assert changed is True

    rows = log.read_all()
    messages = entries_to_messages(rows)
    assert "compacted" in messages[0]["content"]
    # 最近若干轮应该原样保留（不是摘要文本）
    tail_user_msgs = [m["content"] for m in messages[1:] if m["role"] == "user"]
    assert any(c.startswith("round ") for c in tail_user_msgs)


def test_cumulative_summary_passes_previous_summary_to_prompt(tmp_path):
    log = _log(tmp_path)
    for i in range(4):
        _seed_round(log, i, tool_name="bash", content_len=200)

    llm = ScriptedSummaryLLM(["## Goal\nfirst summary"])
    asyncio.run(
        run_summary_compaction(log, log.read_all(), llm, "full_compact", context_window=2000)
    )

    for i in range(4, 6):
        _seed_round(log, i, tool_name="bash", content_len=200)

    llm2 = ScriptedSummaryLLM(["## Goal\nmerged summary"])
    changed = asyncio.run(
        run_summary_compaction(log, log.read_all(), llm2, "full_compact", context_window=2000)
    )
    assert changed is True
    prompt = llm2.calls[0]["messages"][1]["content"]
    assert "first summary" in prompt  # <previous_summary> 被带入了新的摘要请求

    rows = log.read_all()
    summary_rows = [r for r in rows if r.get("type") == "compaction_summary"]
    assert len(summary_rows) == 2
    messages = entries_to_messages(rows)
    assert "merged summary" in messages[0]["content"]
    assert "first summary" not in messages[0]["content"]  # 投影只用最新一条摘要


def test_summary_compaction_noop_when_nothing_to_summarize(tmp_path):
    log = _log(tmp_path)
    _seed_round(log, 0, tool_name="bash", content_len=200)

    llm = ScriptedSummaryLLM(["should not be used"])
    changed = asyncio.run(
        run_summary_compaction(log, log.read_all(), llm, "full_compact", context_window=2000)
    )
    # 只有一轮，full_compact 的 keep_tail_start 就是这一轮本身，没有可摘要内容
    assert changed is False
    assert llm.calls == []


# ---------------------------------------------------------------------------
# maybe_compact 接入点
# ---------------------------------------------------------------------------


def test_maybe_compact_noop_below_threshold(tmp_path):
    log = _log(tmp_path)
    _seed_round(log, 0, tool_name="bash", content_len=100)
    messages = entries_to_messages(log.read_all())
    tracker = ContextUsageTracker(context_window=1_000_000)
    tracker.bootstrap(messages)

    llm = ScriptedSummaryLLM([])
    changed = asyncio.run(maybe_compact(log, messages, tracker, llm))
    assert changed is False


def test_maybe_compact_runs_microcompact_when_over_threshold(tmp_path):
    log = _log(tmp_path)
    for i in range(6):
        _seed_round(log, i, tool_name="bash", content_len=1000)
    messages = entries_to_messages(log.read_all())
    tracker = ContextUsageTracker(context_window=6000)
    tracker.bootstrap(messages)
    before_ratio = tracker.usage_ratio()
    assert 0.30 <= before_ratio < 0.80

    llm = ScriptedSummaryLLM([])
    changed = asyncio.run(maybe_compact(log, messages, tracker, llm))
    assert changed is True
    assert llm.calls == []  # microcompact 不应该发起摘要 LLM 调用
    assert any(r.get("type") == "tool_result_redacted" for r in log.read_all())
    # messages 被原地重写（同一个 list 对象），token 用量应该下降
    assert tracker.usage_ratio() < before_ratio
