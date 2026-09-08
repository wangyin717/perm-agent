"""一批 tool_calls 的两阶段执行：准备串行，执行按路径锁并发。

阶段一严格串行：逐个 before_tool + 写 tool_started。unknown/blocked 直接产出错误结果，
不进入阶段二。

阶段二：READ_ONLY=False 且有 path 的调用按解析后的绝对路径建 asyncio.Lock；
任何调用（含只读）命中这些写路径就排队等锁，其余（含 bash、不同路径的读写）直接并发。
asyncio.gather 一次等完，返回顺序 = 原始调用顺序。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Sequence

from agent_loop.tool_runtime import PreparedCall, ToolRuntime
from agent_loop.tools.records import ToolResultEntry, create_error_tool_result, new_result_id


def terminate_cutoff(results: Sequence[ToolResultEntry]) -> int:
    """保留到第一条 terminate（含）为止的条数。没有 terminate 则全部保留。"""
    for i, result in enumerate(results):
        if result.terminate:
            return i + 1
    return len(results)


async def run_tool_calls(
    runtime: ToolRuntime,
    tool_calls: Sequence[Dict[str, Any]],
    sandbox=None,
) -> List[ToolResultEntry]:
    """跑完一批 tool_calls，返回与输入等长、同序的结果。"""
    prepared: List[PreparedCall] = []
    for tool_call in tool_calls:
        prepared.append(await runtime._prepare_call(tool_call))

    write_locks: Dict[str, asyncio.Lock] = {}
    for item in prepared:
        if item.result is not None or not item.lock_path or item.tool is None:
            continue
        if getattr(item.tool, "READ_ONLY", True):
            continue
        write_locks.setdefault(item.lock_path, asyncio.Lock())

    async def _run_one(item: PreparedCall) -> ToolResultEntry:
        if item.result is not None:
            return item.result
        lock = write_locks.get(item.lock_path) if item.lock_path else None
        try:
            if lock is None:
                return await runtime._execute_prepared(item, sandbox)
            async with lock:
                return await runtime._execute_prepared(item, sandbox)
        except Exception as exc:
            logging.exception(
                "[tool_concurrency] execute 未收口 tool=%s call_id=%s",
                item.name,
                item.call_id,
            )
            result_id = item.started.result_id if item.started else new_result_id()
            return runtime.record_tool_result(
                create_error_tool_result(
                    result_id=result_id,
                    tool_call_id=item.call_id,
                    tool_name=item.name,
                    message=str(exc),
                )
            )

    gathered = await asyncio.gather(
        *[_run_one(item) for item in prepared],
        return_exceptions=True,
    )
    results: List[ToolResultEntry] = []
    for item, outcome in zip(prepared, gathered):
        if isinstance(outcome, Exception):
            logging.exception(
                "[tool_concurrency] gather 异常 tool=%s call_id=%s",
                item.name,
                item.call_id,
            )
            result_id = item.started.result_id if item.started else new_result_id()
            results.append(
                runtime.record_tool_result(
                    create_error_tool_result(
                        result_id=result_id,
                        tool_call_id=item.call_id,
                        tool_name=item.name,
                        message=str(outcome),
                    )
                )
            )
        else:
            results.append(outcome)
    return results
