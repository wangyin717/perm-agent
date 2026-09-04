"""从 jsonl 判断崩溃停在哪，并关上未完成的工具工单。

只看磁盘：有 started 没有同 result_id 的 result → 未关工单。
重放条件：record.replay == "safe" 且当前工具 REPLAY == "safe"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_loop.tool_runtime import ToolRuntime
from agent_loop.tools.records import ToolResultEntry, create_error_tool_result
from agent_loop.tools.registry import get_tool


@dataclass
class UnfinishedTool:
    result_id: str
    tool_call_id: str
    tool_name: str
    effective_args: Dict[str, Any]
    replay: str


@dataclass
class RecoveryPlan:
    unfinished: List[UnfinishedTool] = field(default_factory=list)
    resume_tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    last_entry: Optional[Dict[str, Any]] = None
    # close_unfinished | resume_tools | continue_llm | idle | start_llm | empty
    action: str = "empty"


def inspect_log(rows: List[Dict[str, Any]]) -> RecoveryPlan:
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

    last_entry = None
    for row in reversed(rows):
        if row.get("kind") == "entry":
            last_entry = row
            break

    resume_tool_calls = []
    if last_entry and last_entry.get("type") == "assistant":
        for tool_call in last_entry.get("tool_calls") or []:
            call_id = (tool_call.get("id") or "")
            if call_id and call_id not in started_call_ids and call_id not in result_call_ids:
                resume_tool_calls.append(tool_call)

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
    """最后一次 step_attempt 若还没有同 id 的 assistant entry，返回这个 id。"""
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


def entries_to_messages(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """jsonl 里的 entry 投影成 LLM messages。record 不进上下文。"""
    messages: List[Dict[str, Any]] = []
    for row in rows:
        if row.get("kind") != "entry":
            continue
        entry_type = row.get("type")
        if entry_type == "user":
            messages.append({"role": "user", "content": row.get("content") or ""})
        elif entry_type == "assistant":
            msg: Dict[str, Any] = {
                "role": "assistant",
                "content": row.get("content") or "",
            }
            if row.get("tool_calls"):
                msg["tool_calls"] = row["tool_calls"]
            messages.append(msg)
        elif entry_type == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": row.get("tool_call_id") or "",
                    "name": row.get("tool_name") or "",
                    "content": row.get("content") or "",
                }
            )
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
    for item in unfinished:
        if can_replay(item):
            await _replay_one(runtime, item, sandbox)
        else:
            _interrupt_one(runtime, item)


async def apply_recovery(runtime: ToolRuntime, plan: RecoveryPlan, sandbox=None) -> None:
    await apply_unfinished(runtime, plan.unfinished, sandbox)
    if plan.action == "resume_tools":
        for tool_call in plan.resume_tool_calls:
            await runtime.call_tool(tool_call, sandbox)


async def _replay_one(runtime: ToolRuntime, item: UnfinishedTool, sandbox=None) -> None:
    tool = get_tool(item.tool_name)
    if tool is None:
        _interrupt_one(runtime, item)
        return
    logging.info("[recover] replay tool=%s result_id=%s", item.tool_name, item.result_id)
    try:
        content = await tool.execute(item.effective_args, sandbox)
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
