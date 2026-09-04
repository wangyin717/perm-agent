"""已接入循环的工具表。加工具：实现 NAME / BEFORE_HOOKS / execute，再登记到 TOOLS。"""

from __future__ import annotations

from typing import Any, Optional

from agent_loop.tools import bash_tool

TOOLS = {
    bash_tool.NAME: bash_tool,
}


def get_tool(name: str) -> Optional[Any]:
    return TOOLS.get(name)


def available_tool_names() -> str:
    return ", ".join(TOOLS.keys()) or "(none)"
