"""一次工具调用的两类记账：Record 是 intent，Entry 是对话结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from uuid import uuid4


def new_result_id() -> str:
    return uuid4().hex


@dataclass
class StepAttemptRecord:
    """即将打 LLM 的 intent。assistant entry 的 id 必须等于 result_entry_id。"""

    result_entry_id: str
    step: str = "assistant"
    attempt: int = 1


@dataclass
class ToolStartedRecord:
    """执行前的 intent：args 已定，即将跑。不进模型上下文。"""

    tool_call_id: str
    tool_name: str
    effective_args: Dict[str, Any]
    replay: str = "never"  # "safe" | "never"，写盘时从当时的工具声明拷贝
    result_id: str = field(default_factory=new_result_id)


@dataclass
class ToolOutput:
    """execute 的可选返回值：普通工具继续 return str。"""

    content: str
    media: Optional[Dict[str, Any]] = None


@dataclass
class ToolResultEntry:
    """执行后写入会话的条目。成功路径必须沿用 started.result_id。"""

    result_id: str
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    terminate: bool = False
    media: Optional[Dict[str, Any]] = None

    def to_message(self) -> Dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.tool_name,
            "content": self.content,
        }


def create_error_tool_result(
    *,
    result_id: str,
    tool_call_id: str,
    tool_name: str,
    message: str,
    terminate: bool = False,
) -> ToolResultEntry:
    """所有失败路径（未知工具 / block / execute throw / after_tool throw / interrupted）都走这里。"""
    return ToolResultEntry(
        result_id=result_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        content=message,
        is_error=True,
        terminate=terminate,
    )
