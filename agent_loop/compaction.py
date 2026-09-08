"""按 context_window 占比分级压缩上下文。

三档，命中多档只执行最高一档：
  30%  microcompact  —— 清理"最近尾部"之外、REPLAY=="safe" 的旧 tool_result，
                        替换成占位符（可重新调用工具恢复）。不改变对话结构。
  80%  half_summary  —— 前半段送 LLM 摘要（累积式，带上一次摘要），保留最近尾部原样。
  90%  full_compact  —— 同 half_summary，但只保留最后一轮完整对话，其余全摘要。

同一档在会话生命周期里反复命中是预期行为（microcompact 清理对象耗尽后自然退化成
no-op，ratio 继续升高会自动升级到更高档）。
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

PROTECTED_TAIL_RATIO = 0.15  # microcompact 永不触碰的最近尾部占比
HALF_SUMMARY_TAIL_RATIO = 0.40  # half_summary 摘要后保留的最近尾部占比
MIN_REDACT_CHARS = 800  # 短于此长度的 tool_result 不值得清理
SUMMARY_MAX_TOKENS = 4000  # 摘要请求本身的输出长度上限（防止摘要越摘越长）
TOOL_RESULT_MAX_CHARS = 2000  # 摘要请求里单条 tool_result 的截断长度

# 必须按阈值从高到低排列——pick_level 遍历时第一个命中的就是答案，
# 顺序反了会导致"只执行最高一档"这条规则失效。
COMPACTION_LEVELS = [
    (0.90, "full_compact"),
    (0.80, "half_summary"),
    (0.30, "microcompact"),
]

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

    真实 usage 只在每次 llm.call 之后才知道；两次真实调用之间新追加的消息
    （steer / assistant+tool_calls / tool_result）用 chars//4 的启发式估算补上。
    """

    context_window: int
    known_tokens: int = 0  # 当前"已知"的 token 用量，真实 usage 和本地估算混合更新

    def bootstrap(self, messages: List[Dict[str, Any]]) -> None:
        """从头对一份 messages 全量估算，不发请求。用在会话刚加载完，或者刚压缩完
        （压缩后不想为了知道新用量而专门打一次 LLM，直接本地重新估算最快）。
        """
        self.known_tokens = sum(estimate_tokens(m) for m in messages)

    def update_from_response(self, response) -> None:
        """每次真实 llm.call 之后调用，用 API 返回的真实 usage 覆盖掉本地估算——
        校准累积误差，两次真实调用之间的估算偏差不会一直累加下去。
        """
        used = (response.prompt_tokens or 0) + (response.completion_tokens or 0)
        if used:
            self.known_tokens = used

    def add_estimate(self, message: Dict[str, Any]) -> None:
        """两次真实 llm.call 之间，每 append 一条新消息（steer/assistant/tool_result）
        就调一次，让 known_tokens 能实时反映"发下一次请求前大概会有多大"。
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


def pick_level(ratio: float) -> Optional[str]:
    """COMPACTION_LEVELS 按阈值从高到低排列，第一个 ratio 达到的阈值就是命中的档位——
    这就是"同时越过多条线，只执行最高一档"的实现方式，不需要额外的优先级判断。
    """
    for threshold, level in COMPACTION_LEVELS:
        if ratio >= threshold:
            return level
    return None


async def maybe_compact(
    log: SessionLog,
    messages: list,
    tracker: ContextUsageTracker,
    llm,
    abort: Optional[Abort] = None,
) -> bool:
    """压缩的唯一入口。在下一次 llm.call 之前调用。

    命中阈值则原地重写 messages（`messages[:] = ...`，保留同一个 list 对象，
    这样调用方持有的引用不会失效）并返回 True；未命中或压缩无实际效果返回 False。
    这里只负责"判断该做什么、调对应的 run_* 函数、把结果重新投影回 messages"，
    具体怎么清理/怎么摘要交给下面两个函数。
    """
    ratio = tracker.usage_ratio()
    level = pick_level(ratio)
    if level is None:
        logging.debug("[compaction] ratio=%.3f below 30%% threshold, skip", ratio)
        return False

    logging.info(
        "[compaction] ratio=%.3f (known_tokens=%d/%d) hit level=%s",
        ratio,
        tracker.known_tokens,
        tracker.context_window,
        level,
    )
    rows = log.read_all()

    if level == "microcompact":
        # microcompact 只读写 jsonl、不调 LLM，同步执行即可。
        cleared = run_microcompact(log, rows, tracker.context_window)
        changed = cleared > 0
        if not changed:
            # 可清理的旧结果已经耗尽：这次是空转，ratio 会继续涨，下次自然升级到
            # half_summary/full_compact——不需要在这里维护"升级"状态。
            logging.info("[compaction] microcompact found nothing to redact, no-op")
    else:
        changed = await run_summary_compaction(
            log, rows, llm, level, tracker.context_window, abort=abort
        )

    if not changed:
        return False

    # 压缩记录已经落盘，重新走一遍完整投影拿到压缩后的样子，再重新本地估算校准 tracker
    # （压缩后的用量不能沿用旧的 known_tokens，必须重算）。
    messages[:] = entries_to_messages(log.read_all())
    tracker.bootstrap(messages)
    logging.info(
        "[compaction] %s done: %.3f -> %.3f (known_tokens=%d)",
        level,
        ratio,
        tracker.usage_ratio(),
        tracker.known_tokens,
    )
    log_compaction(level, ratio, tracker.usage_ratio())
    return True


# ---------------------------------------------------------------------------
# 轮次边界 / 尾部预算：microcompact 和摘要共用同一套切分逻辑
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

    只给「会整段删除消息」的场景用（half_summary/full_compact）——删除整条消息可能
    拆散 tool_call/tool_result 配对，必须对齐轮次边界。
    """
    if not entries:
        return 0
    if tail_budget_tokens <= 0:
        return len(entries)
    cutoff = _raw_tail_cutoff(entries, tail_budget_tokens)
    candidates = [s for s in round_starts if s <= cutoff]
    return max(candidates) if candidates else 0


# ---------------------------------------------------------------------------
# microcompact
# ---------------------------------------------------------------------------


def run_microcompact(log: SessionLog, rows: List[Dict[str, Any]], context_window: int) -> int:
    """清理 protected tail 之外、REPLAY=="safe" 的旧 tool_result。返回清理条数。"""
    if not context_window:
        return 0
    covers_upto_seq, _ = latest_compaction_summary(rows)
    entries = visible_entries(rows, covers_upto_seq)
    redactions = active_redactions(rows, covers_upto_seq)

    # redaction 是就地覆盖 tool_result 内容，不删消息、不拆 tool_call/tool_result
    # 配对，所以不需要对齐轮次边界，可以用原始 token 预算切点。
    tail_budget = PROTECTED_TAIL_RATIO * context_window
    tail_start = _raw_tail_cutoff(entries, tail_budget)
    logging.debug(
        "[compaction:microcompact] %d visible entries, protected tail starts at index %d "
        "(budget=%.0f tokens)",
        len(entries),
        tail_start,
        tail_budget,
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
# half_summary / full_compact
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
    level: str,
    context_window: int,
    abort: Optional[Abort] = None,
) -> bool:
    """half_summary：摘要前面，保留最近 40% 尾部。full_compact：只保留最后一轮。"""
    covers_upto_seq, previous_summary = latest_compaction_summary(rows)
    entries = visible_entries(rows, covers_upto_seq)
    if not entries:
        logging.info(
            "[compaction:%s] nothing visible after seq=%d, no-op", level, covers_upto_seq
        )
        return False
    redactions = active_redactions(rows, covers_upto_seq)
    round_starts = _round_starts(entries)

    if level == "full_compact":
        keep_tail_start = round_starts[-1] if round_starts else len(entries)
    else:
        tail_budget = HALF_SUMMARY_TAIL_RATIO * context_window
        keep_tail_start = _tail_start_index(entries, round_starts, tail_budget)

    to_summarize = entries[:keep_tail_start]
    if not to_summarize:
        logging.info(
            "[compaction:%s] tail budget keeps all %d visible entries, nothing to summarize",
            level,
            len(entries),
        )
        return False

    logging.info(
        "[compaction:%s] summarizing %d/%d visible entries (keeping tail of %d), "
        "cumulative=%s",
        level,
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
        "[compaction:%s] dispatching standalone summarization request (%d chars transcript, "
        "not sharing main conversation's messages/cache prefix)",
        level,
        len(transcript),
    )
    response = await llm.call(
        summary_messages, tools=None, abort=abort, max_tokens=SUMMARY_MAX_TOKENS
    )
    summary_text = (response.text or "").strip()
    if not summary_text:
        logging.warning("[compaction:%s] summarization LLM call returned empty text, no-op", level)
        return False

    covers_upto = int(to_summarize[-1].get("seq") or 0)
    log.append_entry(
        "compaction_summary",
        content=summary_text,
        covers_upto_seq=covers_upto,
        level=level,
    )
    logging.info(
        "[compaction:%s] wrote compaction_summary covering seq<=%d (%d chars)",
        level,
        covers_upto,
        len(summary_text),
    )
    return True
