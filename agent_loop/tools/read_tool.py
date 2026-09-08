"""读文本文件。失败 throw，由 ToolRuntime 写成 is_error。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

NAME = "read"
REPLAY = "safe"
READ_ONLY = True

MAX_LINES = 1000
MAX_BYTES = 100_000


def resolve_path(path: str, workspace: str) -> Path:
    """相对路径拼到 workspace；绝对路径原样。不限制出 workspace。"""
    root = Path(workspace or os.getcwd()).resolve()
    raw = Path(path)
    return (raw if raw.is_absolute() else root / raw).resolve()


def before_tool_deny_empty_path(event: Dict[str, Any]):
    path = ((event.get("args") or {}).get("path") or "").strip()
    if not path:
        return {"block": {"reason": "path 为空"}}
    return None


def after_tool_truncate_output(event: Dict[str, Any]):
    content = event.get("content") or ""
    limit = 8000
    if len(content) <= limit:
        return None
    return {"content": content[:limit] + "\n...[output truncated]"}


BEFORE_HOOKS = [before_tool_deny_empty_path]
AFTER_HOOKS = [after_tool_truncate_output]


async def execute(args: Dict[str, Any], sandbox=None, workspace=None) -> str:
    path = (args.get("path") or "").strip()
    if not path:
        raise ValueError("path 为空")
    offset = args.get("offset")
    limit = args.get("limit")
    root = workspace or os.getcwd()
    file_path = resolve_path(path, root)
    if not file_path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    if file_path.is_dir():
        raise IsADirectoryError(f"is a directory: {path}")
    return await _read_text(file_path, offset=offset, limit=limit)


async def _read_text(file_path: Path, offset=None, limit=None) -> str:
    data = file_path.read_bytes()
    if b"\x00" in data[:8000]:
        raise ValueError(f"binary file cannot be read as text: {file_path.name}")
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    start = 0
    if offset is not None:
        start = max(int(offset) - 1, 0)
    if start >= total:
        raise ValueError(f"offset {offset} is beyond end of file ({total} lines)")
    end = total
    if limit is not None:
        end = min(start + max(int(limit), 0), total)
    selected = lines[start:end]
    encoded = "\n".join(selected).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        cut = selected
        while cut and len("\n".join(cut).encode("utf-8")) > MAX_BYTES:
            cut = cut[:-1]
        selected = cut
        end = start + len(selected)
    if len(selected) > MAX_LINES:
        selected = selected[:MAX_LINES]
        end = start + len(selected)
    numbered = []
    for i, line in enumerate(selected, start=start + 1):
        numbered.append(f"{i:6d}|{line}")
    body = "\n".join(numbered)
    shown_start = start + 1
    shown_end = start + len(selected)
    footer = f"\n\n[Showing lines {shown_start}-{shown_end} of {total}]"
    if shown_end < total:
        footer += f" Use offset={shown_end + 1} to continue."
    return body + footer
