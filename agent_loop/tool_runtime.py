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
        """一次工具调用的完整流水线：
        未知工具 / before_tool 拦截 → 直接出错，不落 tool_started（没有"开始跑"过）
        → tool_started（落盘，标记这个工单已经开始跑，崩溃恢复靠它判断）
        → execute（真正跑，异常也要落成错误结果，不能让异常往上抛丢了这次工单）
        → after_tool（hook 可以改写结果内容/是否报错/是否终止本轮）
        → tool_result（落盘关单，同一个 result_id 从 started 传到这里）
        """
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

        # 从这里开始才算"真的要跑了"：before_tool 可能已经改过 args（decision.args），
        # 后面 execute/重放都必须用这份改过的 effective_args，不能再看原始 args。
        started = self.record_tool_started(
            call_id, name, decision.args, _tool_replay(tool)
        )
        try:
            content = await tool.execute(
                started.effective_args, sandbox, workspace=self.workspace
            )
            is_error = False
        except Exception as exc:
            # execute 允许直接 throw；这里统一收口成错误结果，循环不会因为异常中断。
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
        """写 kind=record（不进模型上下文），生成新 result_id。
        这个 result_id 会一路传到 record_tool_result，是判断"工单开着没关"的钥匙——
        inspect_log 靠"有 started 没有同 result_id 的 result"识别未完成工单。
        """
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
        """写 kind=entry（进模型上下文），用传入的 result_id 关单——
        成功路径必须沿用 started.result_id；失败路径（未知工具/block）自己生成新 id，
        因为那些路径压根没有对应的 started 记录。
        """
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
