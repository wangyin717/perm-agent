"""终端上可读的 loop 过程。每条日志一行，过长截断。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

_WS = re.compile(r"\s+")
_TAG_WIDTH = 9  # 对齐 [session] / [recover]
_LINE = 120
_PRIMARY_ARGS = ("cmd", "path", "query", "pattern")
_SKIP_EXTRAS = {"content", "contents"}


def one_line(text: Any, limit: int = _LINE) -> str:
    """换行和连续空白收成单个空格，超出截断。"""
    raw = text if isinstance(text, str) else str(text)
    raw = _WS.sub(" ", raw.replace("\r", "\n")).strip()
    if len(raw) <= limit:
        return raw
    return f"{raw[:limit]}...({len(raw)} chars)"


def clip(text: Any, limit: int = _LINE) -> str:
    return one_line(text, limit)


def summarize_args(args: Dict[str, Any], limit: int = _LINE) -> str:
    """bash 只打 cmd，read 只打 path；其余压成一行 json。"""
    if not args:
        return ""
    for key in _PRIMARY_ARGS:
        if key not in args:
            continue
        extras = [
            f"{k}={v}"
            for k, v in args.items()
            if k != key and k not in _SKIP_EXTRAS
        ]
        body = str(args[key])
        if extras:
            body = f"{body} ({', '.join(extras)})"
        return one_line(body, limit)
    try:
        dumped = json.dumps(args, ensure_ascii=False)
    except Exception:
        dumped = str(args)
    return one_line(dumped, limit)


def format_args(args: Dict[str, Any], limit: int = _LINE) -> str:
    return summarize_args(args, limit)


def summarize_result(content: str, limit: int = _LINE) -> str:
    for ln in (content or "").splitlines():
        s = ln.strip()
        if s:
            return one_line(s, limit)
    return "(empty)"


def _emit(tag: str, msg: str) -> None:
    logging.info("%-*s %s", _TAG_WIDTH, f"[{tag}]", msg)


def log_session(session_id: str, relpath: str) -> None:
    _emit("session", f"{session_id}  {relpath}")


def log_user(text: str) -> None:
    _emit("user", one_line(text, 160))


def log_llm_text(text: str) -> None:
    preview = one_line(text, _LINE)
    _emit("llm", f"reply  {preview}" if preview else "reply")


def log_llm_tools(tool_calls: list) -> None:
    for call in tool_calls or []:
        fn = call.get("function") or {}
        name = fn.get("name") or "?"
        raw = fn.get("arguments") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                _emit("llm", f"{name}  {one_line(raw)}")
                continue
        if isinstance(raw, dict):
            _emit("llm", f"{name}  {summarize_args(raw)}")
        else:
            _emit("llm", f"{name}  {one_line(raw)}")


def log_tool_result(name: str, content: str, is_error: bool) -> None:
    tag = "error" if is_error else "ok"
    _emit("tool", f"{name}  {tag}  {summarize_result(content)}")
