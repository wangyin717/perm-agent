"""终端过程日志：按 User / Assistant / Tools 分段，和对话记录一个结构。"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any, Dict, List, Optional

_WS = re.compile(r"\s+")
_LINE = 120
_TOOL_LINE = 200
_PRIMARY_ARGS = ("cmd", "path", "query", "pattern")
_SKIP_EXTRAS = {"content", "contents"}
_DISPLAY_NAMES = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "grep": "Grep",
}


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


def _tool_label(name: str) -> str:
    if name in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[name]
    if not name:
        return "?"
    return name[:1].upper() + name[1:]


def _parse_tool_args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    if raw is None:
        return {}
    return {"_raw": raw}


def format_tool_line(name: str, args: Optional[Dict[str, Any]] = None) -> str:
    """一条 Tools bullet。Read 带行号范围；Write 只打 path；Bash 只打 cmd。"""
    args = args or {}
    label = _tool_label(name)
    if name == "read":
        path = str(args.get("path") or "")
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is None and limit is None:
            return f"- {label}: {path}" if path else f"- {label}"
        start = int(offset) if offset is not None else 1
        if limit is not None:
            end = start + max(int(limit), 0) - 1
            return f"- {label}: {path} ({start}-{end})"
        return f"- {label}: {path} ({start}-)"
    if name == "write" or name == "edit":
        path = str(args.get("path") or "")
        return f"- {label}: {path}" if path else f"- {label}"
    if name == "grep":
        pattern = one_line(args.get("pattern") or "", _TOOL_LINE)
        extras = [str(args[k]) for k in ("path", "glob") if args.get(k)]
        if extras and pattern:
            pattern = f"{pattern} ({', '.join(extras)})"
        elif extras and not pattern:
            pattern = ", ".join(extras)
        return f"- {label}: {pattern}" if pattern else f"- {label}"
    if name == "bash":
        cmd = args.get("cmd") or args.get("command") or ""
        body = one_line(cmd, _TOOL_LINE)
        return f"- {label}: {body}" if body else f"- {label}"
    if "_raw" in args and len(args) == 1:
        return f"- {label}: {one_line(args['_raw'], _TOOL_LINE)}"
    summary = summarize_args(args, _TOOL_LINE)
    if summary:
        return f"- {label}: {summary}"
    return f"- {label}"


def format_tool_line_from_call(call: Dict[str, Any]) -> str:
    fn = call.get("function") or {}
    name = fn.get("name") or call.get("name") or "?"
    return format_tool_line(name, _parse_tool_args(fn.get("arguments")))


def _emit(text: str) -> None:
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _section(title: str, body: str = "") -> None:
    body = (body or "").rstrip()
    if body:
        _emit(f"## {title}\n\n{body}\n\n")
    else:
        _emit(f"## {title}\n\n")


def log_session(session_id: str, relpath: str) -> None:
    _emit(f"session {session_id}  {relpath}\n\n")


def log_user(text: str) -> None:
    _section("User", text or "")


def log_llm_text(text: str) -> None:
    body = (text or "").rstrip()
    if not body:
        return
    _section("Assistant", body)


def log_llm_tools(tool_calls: list) -> None:
    calls: List[Dict[str, Any]] = list(tool_calls or [])
    if not calls:
        return
    lines = [format_tool_line_from_call(call) for call in calls]
    _section("Tools", "\n".join(lines))


def log_tool_result(name: str, content: str, is_error: bool) -> None:
    """工具结果只进 jsonl；终端保持 Tools 列表干净。"""
    tag = "error" if is_error else "ok"
    logging.debug("tool %s %s %s", name, tag, summarize_result(content))
