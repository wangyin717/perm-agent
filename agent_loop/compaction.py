"""按 context_window 占比压缩上下文。两级，同一集里做完再打主模型。

检查时机（对齐 Pi）：回合结束后看 ≥80% 再压，好让这一回合的请求继续吃 KV cache。
回合中途只在下一枪估算已经 ≥100%（会超窗）时才压。

  ≥80%  回合结束进场。只追加、不改历史，直到这条线。
  microcompact  先清最近 10% 之外、REPLAY=="safe" 的旧 tool_result，换成占位符。
  ≤30%  收工。micro 之后够瘦就不再摘要。
  summary       仍 >30%：前面变摘要，最近 10% 完整轮次原样留（至少一轮）。
  仍 >30%       最后一轮自己太大，认栽继续，不再压。

micro 和 summary 共用同一个 keep 切点，prelude 不会 redact 准备留下的尾巴。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent_loop.abort import Abort
from agent_loop.recover import (
    active_redactions,
    entries_to_messages,
    latest_compaction_summary,
    visible_entries,
)
from agent_loop.session_log import SessionLog
from agent_loop.tools.registry import get_tool
from agent_loop.trace import log_compaction, summarize_args

TRIGGER_RATIO = 0.80  # 回合结束：低于这条线不改已经发出去的前缀
OVERFLOW_RATIO = 1.00  # 回合中途：下一枪估算达到窗口才压
SUCCESS_RATIO = 0.30  # 压到这条线以下就收工
KEEP_TAIL_RATIO = 0.10  # 最近这段完整轮次原样保留（窗口占比，对齐到 user 边界）
MIN_REDACT_CHARS = 800  # 短于此长度的 tool_result 不值得清理
SUMMARY_MAX_TOKENS = 4000  # 摘要请求本身的输出长度上限（防止摘要越摘越长）
TOOL_RESULT_MAX_CHARS = 2000  # 摘要请求里单条 tool_result 的截断长度

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant for a coding agent. Summarize the "
    "conversation transcript below. Do NOT continue the conversation, answer any "
    "question in it, or call any tool. Output ONLY the structured summary."
)

SUMMARIZATION_TEMPLATE = """Summarize the following coding agent conversation. Preserve concrete details a developer would need to keep working: the user's goal and constraints, files read/written/edited (with paths), commands run and their outcomes, decisions made, and what remains to be done.

Output using this exact structure:
## Goal
## Progress (done / in progress / blocked)
## Key decisions
## Files touched
## Next steps

<transcript>
{transcript}
</transcript>"""

UPDATE_SUMMARIZATION_TEMPLATE = """Here is a previous summary of this coding agent conversation, followed by the next chunk of transcript. Produce an UPDATED summary that merges both — integrate, don't just append.

Keep the same structure:
## Goal
## Progress (done / in progress / blocked)
## Key decisions
## Files touched
## Next steps

<previous_summary>
{previous_summary}
</previous_summary>

<new_transcript>
{transcript}
</new_transcript>"""


@dataclass
class ContextUsageTracker:
    """维护"当前上下文大概用了多少 token"的滚动估算。

    真实 usage 只在每次 llm.call 之后才知道。update_from_response 用
    prompt+completion 覆盖 known_tokens——completion 就是即将进入下一枪
    prompt 的 assistant 消息。两次真实调用之间只把 completion 里没有的
    新消息（steer / tool_result）用 chars//4 补上，不要再估一遍 assistant。
    """

    context_window: int
    known_tokens: int = 0  # 当前"已知"的 token 用量，真实 usage 和本地估算混合更新

    def bootstrap(self, messages: List[Dict[str, Any]]) -> None:
        """从头对一份 messages 全量估算，不发请求。用在会话刚加载完，或者刚压缩完
        （压缩后不想为了知道新用量而专门打一次 LLM，直接本地重新估算最快）。
        """
        self.known_tokens = sum(estimate_tokens(m) for m in messages)

    def update_from_response(self, response) -> None:
        """每次真实 llm.call 之后调用，用 API 返回的真实 usage 覆盖掉本地估算。

        known_tokens = prompt + completion。这已经是「旧 prompt + 本枪 assistant」
        的下一枪基数；校准累积误差，两次真实调用之间的估算偏差不会一直累加。
        """
        used = (response.prompt_tokens or 0) + (response.completion_tokens or 0)
        if used:
            self.known_tokens = used

    def add_estimate(self, message: Dict[str, Any]) -> None:
        """两次真实 llm.call 之间，只给 completion 里没有的新消息补估算
        （steer / tool_result）。assistant 已含在 completion_tokens 里，不要再调。
        """
        self.known_tokens += estimate_tokens(message)

    def usage_ratio(self) -> float:
        if not self.context_window:
            return 0.0
        return self.known_tokens / self.context_window


def estimate_tokens(obj: Any) -> int:
    """chars//4 的粗略启发式（和 grok-build 的做法一致）。不追求精确，只用来在两次
    真实 usage 之间填补空白，误差会在下一次 llm.call 后被 update_from_response 校准掉。
    """
    try:
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        text = str(obj)
    return max(len(text) // 4, 0)


def _reproject(messages: list, log: SessionLog, tracker: ContextUsageTracker) -> None:
    """压缩落盘后重新投影。保留 messages 开头连续的 system，避免把系统提示弄丢。"""
    leading = []
    for msg in messages:
        if msg.get("role") == "system":
            leading.append(msg)
        else:
            break
    messages[:] = leading + entries_to_messages(log.read_all())
    tracker.bootstrap(messages)


async def maybe_compact(
    log: SessionLog,
    messages: list,
    tracker: ContextUsageTracker,
    llm,
    abort: Optional[Abort] = None,
    *,
    overflow_only: bool = False,
) -> bool:
    """压缩的唯一入口。

    overflow_only=False：回合结束，≥80% 进场。
    overflow_only=True：回合中途，只有估算已经 ≥100%（下一枪会超窗）才进场。
    同一集里先 micro，仍 >30% 再 summary。命中则原地重写 messages。
    """
    ratio = tracker.usage_ratio()
    min_ratio = OVERFLOW_RATIO if overflow_only else TRIGGER_RATIO
    if ratio < min_ratio:
        logging.debug(
            "[compaction] ratio=%.3f below %.0f%% (%s), skip",
            ratio,
            min_ratio * 100,
            "overflow" if overflow_only else "turn-end",
        )
        return False

    logging.info(
        "[compaction] ratio=%.3f (known_tokens=%d/%d) enter cascade (%s)",
        ratio,
        tracker.known_tokens,
        tracker.context_window,
        "overflow" if overflow_only else "turn-end",
    )
    changed = False

    cleared = run_microcompact(log, log.read_all(), tracker.context_window)
    if cleared > 0:
        _reproject(messages, log, tracker)
        changed = True
        log_compaction("microcompact", ratio, tracker.usage_ratio())
        logging.info(
            "[compaction] microcompact %.3f -> %.3f (known_tokens=%d)",
            ratio,
            tracker.usage_ratio(),
            tracker.known_tokens,
        )
    else:
        logging.info("[compaction] microcompact found nothing to redact, no-op")

    if tracker.usage_ratio() <= SUCCESS_RATIO:
        return changed

    before_summary = tracker.usage_ratio()
    summarized = await run_summary_compaction(
        log, log.read_all(), llm, tracker.context_window, abort=abort
    )
    if summarized:
        _reproject(messages, log, tracker)
        changed = True
        log_compaction("summary", before_summary, tracker.usage_ratio())
        logging.info(
            "[compaction] summary %.3f -> %.3f (known_tokens=%d)",
            before_summary,
            tracker.usage_ratio(),
            tracker.known_tokens,
        )
    else:
        logging.warning(
            "[compaction] summary did not apply (empty or nothing to keep-cut); "
            "continuing at ratio=%.3f",
            tracker.usage_ratio(),
        )

    if tracker.usage_ratio() > SUCCESS_RATIO:
        logging.info(
            "[compaction] still %.3f after cascade (last round likely too large), stop",
            tracker.usage_ratio(),
        )
    return changed


# ---------------------------------------------------------------------------
# 轮次边界 / 尾部预算：microcompact 和摘要共用同一个切点
# ---------------------------------------------------------------------------


def _round_starts(entries: List[Dict[str, Any]]) -> List[int]:
    """每一轮都以一条 user entry 开头（初始提问或 steer）。返回轮次起始下标。"""
    starts = [0] if entries else []
    for i, row in enumerate(entries):
        if i > 0 and row.get("type") == "user":
            starts.append(i)
    return starts


def _entry_tokens(row: Dict[str, Any]) -> int:
    """排掉 kind/seq/id/result_id 这些簿记字段再估算，避免它们虚高 token 计数——
    这几个字段不会真的发给模型，只存在于 jsonl 里。
    """
    return estimate_tokens(
        {k: v for k, v in row.items() if k not in ("kind", "seq", "id", "result_id")}
    )


def _raw_tail_cutoff(entries: List[Dict[str, Any]], tail_budget_tokens: float) -> int:
    """从末尾往前累计 token，返回第一个≥预算的下标（不对齐轮次边界）。"""
    if not entries:
        return 0
    if tail_budget_tokens <= 0:
        return len(entries)
    acc = 0
    for i in range(len(entries) - 1, -1, -1):
        acc += _entry_tokens(entries[i])
        if acc >= tail_budget_tokens:
            return i
    return 0


def _tail_start_index(
    entries: List[Dict[str, Any]], round_starts: List[int], tail_budget_tokens: float
) -> int:
    """从末尾往前累计 token，找到第一个≥预算的位置，再对齐到不晚于它的轮次起点。

    删除整条消息可能拆散 tool_call/tool_result 配对，必须对齐轮次边界。
    最后一轮即使比预算肥，也会被对齐回去——至少留一轮完整对话。
    """
    if not entries:
        return 0
    if tail_budget_tokens <= 0:
        return len(entries)
    cutoff = _raw_tail_cutoff(entries, tail_budget_tokens)
    candidates = [s for s in round_starts if s <= cutoff]
    return max(candidates) if candidates else 0


def _keep_tail_start(entries: List[Dict[str, Any]], context_window: int) -> int:
    """最近 KEEP_TAIL_RATIO 窗口，对齐到轮次起点。"""
    if not entries or not context_window:
        return 0
    return _tail_start_index(
        entries, _round_starts(entries), KEEP_TAIL_RATIO * context_window
    )


# ---------------------------------------------------------------------------
# microcompact
# ---------------------------------------------------------------------------


def run_microcompact(log: SessionLog, rows: List[Dict[str, Any]], context_window: int) -> int:
    """清理 keep 尾巴之外、REPLAY=="safe" 的旧 tool_result。返回清理条数。"""
    if not context_window:
        return 0
    covers_upto_seq, _ = latest_compaction_summary(rows)
    entries = visible_entries(rows, covers_upto_seq)
    redactions = active_redactions(rows, covers_upto_seq)
    tail_start = _keep_tail_start(entries, context_window)
    logging.debug(
        "[compaction:microcompact] %d visible entries, keep tail starts at index %d",
        len(entries),
        tail_start,
    )

    started_by_result_id = {
        row.get("result_id"): row
        for row in rows
        if row.get("kind") == "record" and row.get("type") == "tool_started"
    }

    cleared = 0
    for row in entries[:tail_start]:
        if row.get("type") != "tool_result":
            continue
        result_id = row.get("result_id") or ""
        if not result_id or result_id in redactions:
            continue
        content = row.get("content") or ""
        if len(content) < MIN_REDACT_CHARS:
            continue
        tool = get_tool(row.get("tool_name") or "")
        if tool is None or getattr(tool, "REPLAY", "never") != "safe":
            continue
        started = started_by_result_id.get(result_id)
        args_hint = summarize_args((started or {}).get("effective_args") or {})
        placeholder = (
            f"[content redacted, call {row.get('tool_name')} again to restore] {args_hint}".strip()
        )
        log.append_entry(
            "tool_result_redacted",
            result_id=result_id,
            content=placeholder,
        )
        logging.debug(
            "[compaction:microcompact] redacted result_id=%s tool=%s (%d chars freed)",
            result_id,
            row.get("tool_name"),
            len(content),
        )
        cleared += 1
    logging.info("[compaction:microcompact] redacted %d/%d old tool_result(s)", cleared, tail_start)
    return cleared


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def _serialize_entry(row: Dict[str, Any], redactions: Dict[str, str]) -> str:
    """把一条 entry 拍扁成给摘要 LLM 看的纯文本。这里用的是给人/模型读的自然语言标签
    （[User]/[Assistant]/[Tool result: ...]），跟 entries_to_messages 投影出的
    OpenAI messages 格式是两套不同的表示，互不影响。
    """
    entry_type = row.get("type")
    if entry_type == "user":
        return f"[User]\n{row.get('content') or ''}"
    if entry_type == "assistant":
        text = row.get("content") or ""
        lines = [f"[Assistant]\n{text}"] if text else ["[Assistant]"]
        for tc in row.get("tool_calls") or []:
            fn = tc.get("function") or {}
            lines.append(f"  tool_call: {fn.get('name')}({fn.get('arguments')})")
        return "\n".join(lines)
    if entry_type == "tool_result":
        content = redactions.get(row.get("result_id") or "", row.get("content") or "")
        if len(content) > TOOL_RESULT_MAX_CHARS:
            content = content[:TOOL_RESULT_MAX_CHARS] + f"...({len(content)} chars)"
        tag = "error" if row.get("is_error") else "ok"
        return f"[Tool result: {row.get('tool_name')} {tag}]\n{content}"
    return ""


async def run_summary_compaction(
    log: SessionLog,
    rows: List[Dict[str, Any]],
    llm,
    context_window: int,
    abort: Optional[Abort] = None,
) -> bool:
    """摘要 keep 切点之前的可见条目，最近 KEEP_TAIL_RATIO（至少一轮）原样留。"""
    if not context_window:
        return False
    covers_upto_seq, previous_summary = latest_compaction_summary(rows)
    entries = visible_entries(rows, covers_upto_seq)
    if not entries:
        logging.info(
            "[compaction:summary] nothing visible after seq=%d, no-op", covers_upto_seq
        )
        return False
    redactions = active_redactions(rows, covers_upto_seq)
    keep_tail_start = _keep_tail_start(entries, context_window)

    to_summarize = entries[:keep_tail_start]
    if not to_summarize:
        logging.info(
            "[compaction:summary] tail budget keeps all %d visible entries, nothing to summarize",
            len(entries),
        )
        return False

    logging.info(
        "[compaction:summary] summarizing %d/%d visible entries (keeping tail of %d), "
        "cumulative=%s",
        len(to_summarize),
        len(entries),
        len(entries) - len(to_summarize),
        bool(previous_summary),
    )
    transcript = "\n\n".join(_serialize_entry(row, redactions) for row in to_summarize)
    if previous_summary:
        prompt = UPDATE_SUMMARIZATION_TEMPLATE.format(
            previous_summary=previous_summary, transcript=transcript
        )
    else:
        prompt = SUMMARIZATION_TEMPLATE.format(transcript=transcript)

    summary_messages = [
        {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    logging.debug(
        "[compaction:summary] dispatching standalone summarization request (%d chars transcript, "
        "not sharing main conversation's messages/cache prefix)",
        len(transcript),
    )
    response = await llm.call(
        summary_messages, tools=None, abort=abort, max_tokens=SUMMARY_MAX_TOKENS
    )
    summary_text = (response.text or "").strip()
    if not summary_text:
        logging.warning("[compaction:summary] summarization LLM call returned empty text, no-op")
        return False

    covers_upto = int(to_summarize[-1].get("seq") or 0)
    log.append_entry(
        "compaction_summary",
        content=summary_text,
        covers_upto_seq=covers_upto,
        level="summary",
    )
    logging.info(
        "[compaction:summary] wrote compaction_summary covering seq<=%d (%d chars)",
        covers_upto,
        len(summary_text),
    )
    return True
