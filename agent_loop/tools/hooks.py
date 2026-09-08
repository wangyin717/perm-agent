"""工具 hook 的执行器。按工具名分发，各工具只跑自己的规则。

before_tool 每个规则三选一：
  1. 返回 None          → 不管，继续
  2. 返回 {"args": ...} → 用新参数去执行
  3. 返回 {"block": {"reason": "..."}} → 拦住，不执行工具

after_tool 可 patch：
  1. 返回 None → 不管
  2. 返回 {"content"/"is_error"/"terminate"} → 写进最终 tool result
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from agent_loop.tools.registry import TOOLS


@dataclass
class BeforeToolDecision:
    blocked: bool
    args: Dict[str, Any]
    reason: str = ""
    message: str = ""  # blocked 时可以直接当 tool result 返回


@dataclass
class AfterToolDecision:
    content: str
    is_error: bool = False
    terminate: bool = False


class Hooks:
    def __init__(self):
        self.before_tool_handlers: Dict[str, List[Callable]] = {
            name: list(getattr(tool, "BEFORE_HOOKS", []) or [])
            for name, tool in TOOLS.items()
        }
        self.after_tool_handlers: Dict[str, List[Callable]] = {
            name: list(getattr(tool, "AFTER_HOOKS", []) or [])
            for name, tool in TOOLS.items()
        }

    def add_before_tool(self, tool_name: str, handler: Callable) -> None:
        self.before_tool_handlers.setdefault(tool_name, []).append(handler)

    def add_after_tool(self, tool_name: str, handler: Callable) -> None:
        self.after_tool_handlers.setdefault(tool_name, []).append(handler)

    async def run_before_tool(
        self,
        tool_call_id: str,
        tool_name: str,
        args: Dict[str, Any],
    ) -> BeforeToolDecision:
        current_args = dict(args or {})
        handlers = self.before_tool_handlers.get(tool_name) or []

        # 链式执行：后一个 handler 看到的是前一个改过的 args，不是原始 args；
        # 任何一个 handler 返回 block 就立即短路，后面的 handler 不会再跑。
        for handler in handlers:
            event = {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "args": current_args,
            }
            result = handler(event)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                continue

            if result.get("block"):
                reason = "blocked"
                block = result["block"]
                if isinstance(block, dict) and block.get("reason"):
                    reason = str(block["reason"])
                message = f"Tool blocked: {reason}"
                logging.info(
                    "[before_tool] block tool=%s reason=%s",
                    tool_name,
                    reason,
                )
                return BeforeToolDecision(
                    blocked=True,
                    args=current_args,
                    reason=reason,
                    message=message,
                )

            new_args = result.get("args")
            if new_args is not None:
                current_args = dict(new_args)

        return BeforeToolDecision(blocked=False, args=current_args)

    async def run_after_tool(
        self,
        tool_call_id: str,
        tool_name: str,
        args: Dict[str, Any],
        content: str,
        is_error: bool = False,
    ) -> AfterToolDecision:
        current_content = content if content is not None else ""
        current_is_error = bool(is_error)
        terminate = False
        handlers = self.after_tool_handlers.get(tool_name) or []

        # 同样是链式：content/is_error 会被逐个 handler 累积改写；terminate 一旦被
        # 某个 handler 置为 True 就不会再被后面的 handler 清掉（这里没有 elif None 分支）。
        for handler in handlers:
            event = {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "args": dict(args or {}),
                "content": current_content,
                "is_error": current_is_error,
            }
            result = handler(event)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                continue
            if result.get("content") is not None:
                current_content = result["content"]
            if result.get("is_error") is not None:
                current_is_error = bool(result["is_error"])
            elif result.get("isError") is not None:
                current_is_error = bool(result["isError"])
            if result.get("terminate") is not None:
                terminate = bool(result["terminate"])

        return AfterToolDecision(
            content=current_content,
            is_error=current_is_error,
            terminate=terminate,
        )
