"""从 jsonl 判断崩溃停在哪，并关上未完成的工具工单。

只看磁盘：有 started 没有同 result_id 的 result → 未关工单。
重放条件：record.replay == "safe" 且当前工具 REPLAY == "safe"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_loop.runtime.tool_runtime import ToolRuntime
from agent_loop.runtime.records import ToolResultEntry, create_error_tool_result
from agent_loop.tools.registry import get_tool


@dataclass
class UnfinishedTool:
    result_id: str
    tool_call_id: str
    tool_name: str
    effective_args: Dict[str, Any]
    replay: str


_META_ENTRY_TYPES = {"tool_result_redacted", "compaction_summary"}


@dataclass
class RecoveryPlan:
    unfinished: List[UnfinishedTool] = field(default_factory=list)
    resume_tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    last_entry: Optional[Dict[str, Any]] = None
    # close_unfinished | resume_tools | continue_llm | idle | start_llm | empty
    action: str = "empty"


def inspect_log(rows: List[Dict[str, Any]]) -> RecoveryPlan:
    """纯函数：只看磁盘上的行，不发任何请求，判断上次进程停在哪一步、接下来该做什么。"""
    started = [
        row
        for row in rows
        if row.get("kind") == "record" and row.get("type") == "tool_started"
    ]
    results = [
        row
        for row in rows
        if row.get("kind") == "entry" and row.get("type") == "tool_result"
    ]
    closed_ids = {row.get("result_id") for row in results if row.get("result_id")}
    started_call_ids = {row.get("tool_call_id") for row in started if row.get("tool_call_id")}
    result_call_ids = {row.get("tool_call_id") for row in results if row.get("tool_call_id")}

    # 有 tool_started 记录、但同 result_id 没有对应 tool_result：说明进程死在
    # execute/hook 执行期间，工单开着没关。
    unfinished = []
    for row in started:
        result_id = row.get("result_id") or ""
        if result_id and result_id not in closed_ids:
            unfinished.append(
                UnfinishedTool(
                    result_id=result_id,
                    tool_call_id=row.get("tool_call_id") or "",
                    tool_name=row.get("tool_name") or "",
                    effective_args=dict(row.get("effective_args") or {}),
                    replay=row.get("replay") or "never",
                )
            )

    # 跳过压缩产生的 meta entry（tool_result_redacted / compaction_summary），
    # 它们不是"对话真的停在这里"，只是压缩记录，不能拿来判断恢复动作。
    last_entry = None
    for row in reversed(rows):
        if row.get("kind") == "entry" and row.get("type") not in _META_ENTRY_TYPES:
            last_entry = row
            break

    # 最后一条是 assistant+tool_calls，但其中某个 tool_call 既没 started 也没 result：
    # 说明进程在"写完 assistant entry"和"调用 call_tool 开始执行"之间的窗口崩溃了，
    # 工具连"开始跑"都没记录下来，需要重新触发 call_tool（而不是走 unfinished 的重放路径）。
    resume_tool_calls = []
    if last_entry and last_entry.get("type") == "assistant":
        for tool_call in last_entry.get("tool_calls") or []:
            call_id = (tool_call.get("id") or "")
            if call_id and call_id not in started_call_ids and call_id not in result_call_ids:
                resume_tool_calls.append(tool_call)

    # 六种恢复动作，按优先级判断（前面命中就不看后面）：
    #   close_unfinished — 有工单开着没关，先关它（可能重放，也可能标 interrupted）
    #   resume_tools     — 工单都关了，但有 tool_call 连开始跑都没记录，补跑
    #   empty            — 全新会话，从头开始
    #   continue_llm     — 最后是 tool_result，模型还没针对它说话，接着打 LLM
    #   start_llm        — 最后是 user，还没打过 LLM
    #   idle             — 最后是纯文本 assistant（正常收尾），等下一句新问题
    if unfinished:
        action = "close_unfinished"
    elif resume_tool_calls:
        action = "resume_tools"
    elif last_entry is None:
        action = "empty"
    elif last_entry.get("type") == "tool_result":
        action = "continue_llm"
    elif last_entry.get("type") == "user":
        action = "start_llm"
    else:
        action = "idle"

    return RecoveryPlan(
        unfinished=unfinished,
        resume_tool_calls=resume_tool_calls,
        last_entry=last_entry,
        action=action,
    )


def unfinished_assistant_id(rows: List[Dict[str, Any]]) -> Optional[str]:
    """最后一次 step_attempt 若还没有同 id 的 assistant entry，返回这个 id。

    用于 _begin_llm_step：如果进程死在 llm.call 内部（还没写 assistant entry），
    下次重启时"许诺过的 id"要被复用，不能凭空再发一个新 id 出来，否则会有一个
    step_attempt record 永远对不上任何 assistant entry。按顺序扫一遍所有
    step_attempt，谁的 id 还没被"兑现"（没同 id 的 assistant entry），谁就是当前
    悬空的那个；一旦兑现了就清掉 open_id，继续找后面的。
    """
    assistant_ids = {
        row.get("id")
        for row in rows
        if row.get("kind") == "entry" and row.get("type") == "assistant" and row.get("id")
    }
    open_id = None
    for row in rows:
        if row.get("kind") == "record" and row.get("type") == "step_attempt":
            entry_id = row.get("result_entry_id") or ""
            if entry_id and entry_id not in assistant_ids:
                open_id = entry_id
            elif entry_id in assistant_ids:
                open_id = None
    return open_id


def latest_compaction_summary(rows: List[Dict[str, Any]]) -> tuple[int, str]:
    """最新一条 compaction_summary 的 (covers_upto_seq, content)；没有则是 (0, "")。"""
    latest_seq = -1
    covers_upto_seq = 0
    summary_content = ""
    for row in rows:
        if row.get("kind") == "entry" and row.get("type") == "compaction_summary":
            seq = int(row.get("seq") or 0)
            if seq >= latest_seq:
                latest_seq = seq
                covers_upto_seq = int(row.get("covers_upto_seq") or 0)
                summary_content = row.get("content") or ""
    return covers_upto_seq, summary_content


def active_redactions(rows: List[Dict[str, Any]], covers_upto_seq: int) -> Dict[str, str]:
    """result_id -> 占位内容，只算切点之后仍然生效的 tool_result_redacted。"""
    redactions: Dict[str, str] = {}
    for row in rows:
        if row.get("kind") != "entry" or row.get("type") != "tool_result_redacted":
            continue
        if int(row.get("seq") or 0) <= covers_upto_seq:
            continue
        redactions[row.get("result_id") or ""] = row.get("content") or ""
    return redactions


def visible_entries(rows: List[Dict[str, Any]], covers_upto_seq: int) -> List[Dict[str, Any]]:
    """kind==entry、非 meta 类型、且在压缩切点之后的条目，原始顺序。"""
    return [
        row
        for row in rows
        if row.get("kind") == "entry"
        and row.get("type") not in _META_ENTRY_TYPES
        and int(row.get("seq") or 0) > covers_upto_seq
    ]


def entries_to_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """jsonl 里的 entry 投影成 LLM messages。record 不进上下文。

    压缩产生的两类 meta entry 在这里生效：
    - compaction_summary：取最新一条，covers_upto_seq 之前的 entry 全部跳过，
      摘要内容作为一条 user 消息插在最前面。
    - tool_result_redacted：就地覆盖同 result_id 的 tool_result 内容，不新增消息。

    同一条 assistant 后面的 tool_result 按 tool_calls 顺序重排（并行完成序可能打乱）。
    若某条 result.terminate=True，只投影到该条为止，并把 assistant.tool_calls
    裁到与保留的 result 对齐。jsonl 本身不截。
    """
    covers_upto_seq, summary_content = latest_compaction_summary(rows)
    redactions = active_redactions(rows, covers_upto_seq)
    if summary_content or redactions:
        logging.debug(
            "[recover] projecting %d raw rows: covers_upto_seq=%d has_summary=%s "
            "redacted_count=%d",
            len(rows),
            covers_upto_seq,
            bool(summary_content),
            len(redactions),
        )

    messages: List[Dict[str, Any]] = []
    if summary_content:
        messages.append(
            {
                "role": "user",
                "content": f"[Summary of earlier conversation (compacted)]\n{summary_content}",
            }
        )

    pending_assistant: Optional[Dict[str, Any]] = None
    pending_results: List[Dict[str, Any]] = []

    def flush_assistant() -> None:
        nonlocal pending_assistant, pending_results
        if pending_assistant is None:
            return
        calls = list(pending_assistant.get("tool_calls") or [])
        by_id = {
            row.get("tool_call_id") or "": row
            for row in pending_results
            if row.get("tool_call_id")
        }
        ordered: List[Dict[str, Any]] = []
        seen = set()
        for call in calls:
            call_id = call.get("id") or ""
            row = by_id.get(call_id)
            if row is not None:
                ordered.append(row)
                seen.add(call_id)
        for row in pending_results:
            call_id = row.get("tool_call_id") or ""
            if call_id not in seen:
                ordered.append(row)
                seen.add(call_id)

        cut = len(ordered)
        for i, row in enumerate(ordered):
            if row.get("terminate"):
                cut = i + 1
                break
        ordered = ordered[:cut]
        kept_ids = {row.get("tool_call_id") for row in ordered}

        msg: Dict[str, Any] = {
            "role": "assistant",
            "content": pending_assistant.get("content") or "",
        }
        if calls:
            trimmed = [call for call in calls if (call.get("id") or "") in kept_ids]
            if trimmed:
                msg["tool_calls"] = trimmed
        messages.append(msg)
        for row in ordered:
            result_id = row.get("result_id") or ""
            content = redactions.get(result_id, row.get("content") or "")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": row.get("tool_call_id") or "",
                    "name": row.get("tool_name") or "",
                    "content": content,
                }
            )
        pending_assistant = None
        pending_results = []

    for row in visible_entries(rows, covers_upto_seq):
        entry_type = row.get("type")
        if entry_type == "user":
            flush_assistant()
            messages.append({"role": "user", "content": row.get("content") or ""})
        elif entry_type == "assistant":
            flush_assistant()
            pending_assistant = row
            if not row.get("tool_calls"):
                flush_assistant()
        elif entry_type == "tool_result":
            if pending_assistant is not None:
                pending_results.append(row)
            else:
                result_id = row.get("result_id") or ""
                content = redactions.get(result_id, row.get("content") or "")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": row.get("tool_call_id") or "",
                        "name": row.get("tool_name") or "",
                        "content": content,
                    }
                )
    flush_assistant()
    return messages


def should_append_user(plan: RecoveryPlan, user_query: str) -> bool:
    """continue_llm：先把上一轮说完，不要夹新 user。
    start_llm 且最后一条 user 就是本轮问题：不要重复写。
    """
    if plan.action == "continue_llm":
        return False
    if plan.action == "start_llm":
        last = plan.last_entry or {}
        return (last.get("content") or "") != (user_query or "")
    return True


def can_replay(item: UnfinishedTool) -> bool:
    """必须两边都是 safe：当时 jsonl 里记的 replay，和现在代码里这个工具的 REPLAY 声明。

    工具代码可能在两次运行之间改过（比如 bash 从 safe 改成 never），只看当时记的
    replay 不够——要用现在的声明重新确认一次，否则可能重放一个已经不再安全的操作。
    """
    if item.replay != "safe":
        return False
    tool = get_tool(item.tool_name)
    current = getattr(tool, "REPLAY", "never") if tool else "never"
    return current == "safe"


async def apply_unfinished(
    runtime: ToolRuntime,
    unfinished: List[UnfinishedTool],
    sandbox=None,
) -> None:
    """收尸开着的工单：能安全重放的重新跑一遍，不能的直接标 interrupted 关掉。"""
    for item in unfinished:
        if can_replay(item):
            await _replay_one(runtime, item, sandbox)
        else:
            _interrupt_one(runtime, item)


async def apply_recovery(runtime: ToolRuntime, plan: RecoveryPlan, sandbox=None) -> None:
    """inspect_log 给出 plan 之后，真正落地执行——这一步会写盘（tool_result entry）。"""
    await apply_unfinished(runtime, plan.unfinished, sandbox)
    if plan.action == "resume_tools":
        for tool_call in plan.resume_tool_calls:
            await runtime.call_tool(tool_call, sandbox)


async def _replay_one(runtime: ToolRuntime, item: UnfinishedTool, sandbox=None) -> None:
    """重新跑一遍这个工具（用当时记的 effective_args），走完整 execute→after_tool 流程，
    最后用原来的 result_id 关单——这样投影出的 messages 里 tool_call 和 tool_result
    还是配对的，只是内容变成了这次重放的结果。
    """
    tool = get_tool(item.tool_name)
    if tool is None:
        _interrupt_one(runtime, item)
        return
    logging.info("[recover] replay tool=%s result_id=%s", item.tool_name, item.result_id)
    try:
        content = await tool.execute(
            item.effective_args,
            sandbox,
            workspace=getattr(runtime, "workspace", None),
            session_dir=getattr(runtime, "session_dir", None),
        )
        is_error = False
    except Exception as exc:
        logging.exception("[recover] replay execute 失败 tool=%s", item.tool_name)
        content = str(exc)
        is_error = True
    try:
        after = await runtime.hooks.run_after_tool(
            item.tool_call_id,
            item.tool_name,
            item.effective_args,
            content,
            is_error=is_error,
        )
    except Exception as exc:
        logging.exception("[recover] replay after_tool 失败 tool=%s", item.tool_name)
        runtime.record_tool_result(
            create_error_tool_result(
                result_id=item.result_id,
                tool_call_id=item.tool_call_id,
                tool_name=item.tool_name,
                message=str(exc),
            )
        )
        return
    if after.is_error:
        runtime.record_tool_result(
            create_error_tool_result(
                result_id=item.result_id,
                tool_call_id=item.tool_call_id,
                tool_name=item.tool_name,
                message=after.content,
                terminate=after.terminate,
            )
        )
        return
    runtime.record_tool_result(
        ToolResultEntry(
            result_id=item.result_id,
            tool_call_id=item.tool_call_id,
            tool_name=item.tool_name,
            content=after.content,
            is_error=False,
            terminate=after.terminate,
        )
    )


def _interrupt_one(runtime: ToolRuntime, item: UnfinishedTool) -> None:
    """不能重放（never 或工具已不再声明 safe）：不猜它到底跑完没有，直接标一条错误
    tool_result 关单，把决定权交给模型（模型看到 interrupted 会自己决定要不要重跑）。
    """
    logging.info(
        "[recover] interrupt tool=%s result_id=%s",
        item.tool_name,
        item.result_id,
    )
    runtime.record_tool_result(
        create_error_tool_result(
            result_id=item.result_id,
            tool_call_id=item.tool_call_id,
            tool_name=item.tool_name,
            message="interrupted",
        )
    )
