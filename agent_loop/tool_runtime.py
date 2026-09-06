"""一次工具调用的开/关单：before_tool → started → execute → after_tool → result。"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from agent_loop.session_log import SessionLog
from agent_loop.tools.hooks import Hooks
from agent_loop.tools.records import (
    ToolResultEntry,
    ToolStartedRecord,
    create_error_tool_result,
    new_result_id,
)
from agent_loop.tools.registry import available_tool_names, get_tool
from agent_loop.trace import log_tool_result


class ToolRuntime:
    def __init__(self):
        self.hooks = Hooks()
        self.tool_started_records: List[ToolStartedRecord] = []
        self.tool_result_records: List[ToolResultEntry] = []
        self._log: Optional[SessionLog] = None
        self.workspace: Optional[str] = None

    def attach_log(self, log: Optional[SessionLog]) -> None:
        self._log = log

    async def call_tool(self, tool_call: Dict[str, Any], sandbox=None) -> ToolResultEntry:
        call_id = tool_call.get("id") or ""
        name = (tool_call.get("function") or {}).get("name") or ""
        args = _parse_args(tool_call)

        tool = get_tool(name)
        if tool is None:
            return self.record_tool_result(
                create_error_tool_result(
                    result_id=new_result_id(),
                    tool_call_id=call_id,
                    tool_name=name,
                    message=f"Unknown tool: {name}。可用工具：{available_tool_names()}",
                )
            )

        decision = await self.hooks.run_before_tool(call_id, name, args)
        if decision.blocked:
            return self.record_tool_result(
                create_error_tool_result(
                    result_id=new_result_id(),
                    tool_call_id=call_id,
                    tool_name=name,
                    message=decision.message,
                )
            )

        started = self.record_tool_started(
            call_id, name, decision.args, _tool_replay(tool)
        )
        try:
            content = await tool.execute(
                started.effective_args, sandbox, workspace=self.workspace
            )
            is_error = False
        except Exception as exc:
            logging.warning("[tool] %s failed: %s", name, exc)
            content = str(exc)
            is_error = True
        try:
            after = await self.hooks.run_after_tool(
                call_id,
                name,
                started.effective_args,
                content,
                is_error=is_error,
            )
        except Exception as exc:
            logging.exception("[ToolRuntime] after_tool 失败 tool=%s", name)
            return self.record_tool_result(
                create_error_tool_result(
                    result_id=started.result_id,
                    tool_call_id=call_id,
                    tool_name=name,
                    message=str(exc),
                )
            )
        log_tool_result(name, after.content, after.is_error)
        if after.is_error:
            return self.record_tool_result(
                create_error_tool_result(
                    result_id=started.result_id,
                    tool_call_id=call_id,
                    tool_name=name,
                    message=after.content,
                    terminate=after.terminate,
                )
            )
        return self.record_tool_result(
            ToolResultEntry(
                result_id=started.result_id,
                tool_call_id=call_id,
                tool_name=name,
                content=after.content,
                is_error=False,
                terminate=after.terminate,
            )
        )

    def record_tool_started(
        self,
        tool_call_id: str,
        tool_name: str,
        effective_args: Dict[str, Any],
        replay: str,
    ) -> ToolStartedRecord:
        record = ToolStartedRecord(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            effective_args=dict(effective_args),
            replay=replay if replay in ("safe", "never") else "never",
        )
        self.tool_started_records.append(record)
        if self._log:
            self._log.append_record(
                "tool_started",
                tool_call_id=record.tool_call_id,
                tool_name=record.tool_name,
                effective_args=record.effective_args,
                replay=record.replay,
                result_id=record.result_id,
            )
        logging.debug(
            "[tool_started] id=%s tool=%s result_id=%s",
            tool_call_id,
            tool_name,
            record.result_id,
        )
        return record

    def record_tool_result(self, entry: ToolResultEntry) -> ToolResultEntry:
        self.tool_result_records.append(entry)
        if self._log:
            self._log.append_entry(
                "tool_result",
                result_id=entry.result_id,
                tool_call_id=entry.tool_call_id,
                tool_name=entry.tool_name,
                content=entry.content,
                is_error=entry.is_error,
                terminate=entry.terminate,
            )
        logging.debug(
            "[tool_result] result_id=%s tool=%s is_error=%s",
            entry.result_id,
            entry.tool_name,
            entry.is_error,
        )
        return entry


def _tool_replay(tool) -> str:
    replay = getattr(tool, "REPLAY", "never")
    return replay if replay in ("safe", "never") else "never"


def _parse_args(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    raw = (tool_call.get("function") or {}).get("arguments", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
