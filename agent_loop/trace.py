"""终端上可读的 loop 过程。过长内容截断。"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict


def clip(text: Any, limit: int = 240) -> str:
    raw = text if isinstance(text, str) else str(text)
    raw = raw.replace("\n", "\\n")
    if len(raw) <= limit:
        return raw
    return f"{raw[:limit]}...({len(raw)} chars)"


def format_args(args: Dict[str, Any], limit: int = 240) -> str:
    try:
        dumped = json.dumps(args, ensure_ascii=False)
    except Exception:
        dumped = str(args)
    return clip(dumped, limit)


def log_user(text: str) -> None:
    logging.info("[user] %s", clip(text, 400))


def log_llm_text(text: str) -> None:
    logging.info("[llm] end_turn %s", clip(text, 400))


def log_llm_tools(tool_calls: list) -> None:
    for call in tool_calls or []:
        fn = call.get("function") or {}
        name = fn.get("name") or "?"
        raw = fn.get("arguments") or "{}"
        if isinstance(raw, dict):
            args_s = format_args(raw)
        else:
            args_s = clip(raw, 240)
        logging.info("[llm] tool_call %s %s", name, args_s)


def log_tool_result(name: str, content: str, is_error: bool) -> None:
    tag = "error" if is_error else "ok"
    logging.info("[tool] %s %s %s", name, tag, clip(content, 400))
