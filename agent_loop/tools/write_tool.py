"""整文件写入。失败 throw，由 ToolRuntime 写成 is_error。"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from agent_loop import events as _events
from agent_loop.tools.diff_view import pack_rows, render_rows, write_hunk_rows
from agent_loop.tools.read_tool import before_tool_deny_empty_path, resolve_path

NAME = "write"
REPLAY = "never"
READ_ONLY = False

BEFORE_HOOKS = [before_tool_deny_empty_path]
AFTER_HOOKS: list = []


async def execute(args: Dict[str, Any], sandbox=None, workspace=None, session_dir=None) -> str:
    path = (args.get("path") or "").strip()
    if not path:
        raise ValueError("path 为空")
    if "content" not in args or args.get("content") is None:
        raise ValueError("content 缺失")
    content = args["content"]
    if not isinstance(content, str):
        content = str(content)

    root = workspace or os.getcwd()
    file_path = resolve_path(path, root)
    if file_path.exists() and file_path.is_dir():
        raise IsADirectoryError(f"is a directory: {path}")

    existed = file_path.is_file()
    old: Optional[str] = None
    if existed:
        try:
            old = file_path.read_text(encoding="utf-8")
        except OSError:
            old = ""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    n_bytes = len(content.encode("utf-8"))
    n_lines = len(content.splitlines())
    action = "overwritten" if existed else "created"
    summary = f"Wrote {n_bytes} bytes ({n_lines} lines) to {path} ({action})"
    rows, extra = write_hunk_rows(old if existed else None, content)
    plain, marked = render_rows(rows, extra)
    if rows or extra:
        _events.emit(
            "write_diff",
            path=path,
            hunk=marked,
            rows=pack_rows(rows),
            extra=extra,
        )
    return f"{summary}\n\n{plain}" if plain else summary
