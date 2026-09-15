"""精确字符串替换。失败 throw，由 ToolRuntime 写成 is_error。"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from agent_loop import events as _events
from agent_loop.tools.diff_view import DIFF_CTX, DIFF_MAX_ROWS, pack_rows, render_rows
from agent_loop.tools.read_tool import before_tool_deny_empty_path, resolve_path

NAME = "edit"
REPLAY = "never"
READ_ONLY = False


def before_tool_deny_empty_old(event: Dict[str, Any]):
    """空 old 语义含糊（Grok 拿来新建文件），这里直接拒，新建走 write。"""
    args = event.get("args") or {}
    if "old" not in args or args.get("old") is None or args.get("old") == "":
        return {"block": {"reason": "old 为空"}}
    return None


BEFORE_HOOKS = [before_tool_deny_empty_path, before_tool_deny_empty_old]
AFTER_HOOKS: list = []


async def execute(args: Dict[str, Any], sandbox=None, workspace=None, session_dir=None) -> str:
    path = (args.get("path") or "").strip()
    if not path:
        raise ValueError("path 为空")
    if "old" not in args or args.get("old") is None:
        raise ValueError("old 缺失")
    if "new" not in args or args.get("new") is None:
        raise ValueError("new 缺失")
    old = args["old"]
    new = args["new"]
    if not isinstance(old, str):
        old = str(old)
    if not isinstance(new, str):
        new = str(new)
    if old == "":
        raise ValueError("old 为空")
    if old == new:
        raise ValueError("old and new are the same")

    replace_all = _truthy(args.get("replace_all"))
    root = workspace or os.getcwd()
    file_path = resolve_path(path, root)
    if not file_path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    if file_path.is_dir():
        raise IsADirectoryError(f"is a directory: {path}")

    data = file_path.read_bytes()
    if b"\x00" in data[:8000]:
        raise ValueError(f"binary file cannot be edited as text: {file_path.name}")
    text = data.decode("utf-8")

    starts = _find_starts(text, old)
    n = len(starts)
    if n == 0:
        raise ValueError(
            f"old not found in {path}. Copy exact text from read (no line numbers)."
        )
    if n > 1 and not replace_all:
        lines = _offsets_to_lines(text, starts)
        shown = ", ".join(str(ln) for ln in lines[:10])
        if len(lines) > 10:
            shown += ", ..."
        raise ValueError(
            f"old found {n} times in {path} (lines {shown}). "
            "Expand old until unique, or set replace_all=true."
        )

    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    file_path.write_text(updated, encoding="utf-8")
    word = "occurrence" if n == 1 else "occurrences"
    summary = f"Replaced {n} {word} in {path}"
    rows, extra = edit_hunk_rows(text, old, new, starts[0])
    if n > 1:
        extra = (extra + " · " if extra else "") + f"… and {n - 1} more"
    plain, marked = render_rows(rows, extra)
    if rows or extra:
        _events.emit(
            "edit_diff",
            path=path,
            hunk=marked,
            rows=pack_rows(rows),
            extra=extra,
        )
    return f"{summary}\n\n{plain}" if plain else summary


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def _find_starts(text: str, old: str) -> List[int]:
    starts: List[int] = []
    pos = 0
    step = len(old)
    while True:
        found = text.find(old, pos)
        if found < 0:
            return starts
        starts.append(found)
        pos = found + step


def _offsets_to_lines(text: str, offsets: List[int]) -> List[int]:
    return [text.count("\n", 0, off) + 1 for off in offsets]


def edit_hunk_rows(
    text: str, old: str, new: str, start: int
) -> Tuple[List[Tuple[str, int, str]], str]:
    lines = text.splitlines()
    if not lines and not text:
        lines = [""]
    i0 = text.count("\n", 0, start)
    old_end = start + len(old)
    i1 = i0 if not old else text.count("\n", 0, max(old_end - 1, start))
    line_beg = text.rfind("\n", 0, start) + 1
    nxt = text.find("\n", old_end)
    line_end = len(text) if nxt < 0 else nxt
    prefix = text[line_beg:start]
    suffix = text[old_end:line_end]
    deleted = lines[i0 : i1 + 1] or [""]
    added = (prefix + new + suffix).splitlines() or [""]
    before = max(0, i0 - DIFF_CTX)
    after_i = i1 + 1
    after = min(len(lines), after_i + DIFF_CTX)
    rows: List[Tuple[str, int, str]] = []
    for i in range(before, i0):
        rows.append(("ctx", i + 1, lines[i]))
    for j, ln in enumerate(deleted):
        rows.append(("del", i0 + 1 + j, ln))
    for j, ln in enumerate(added):
        rows.append(("add", i0 + 1 + j, ln))
    new_ln = i0 + len(added)
    for j, i in enumerate(range(after_i, after)):
        rows.append(("ctx", new_ln + 1 + j, lines[i]))
    extra = ""
    if len(rows) > DIFF_MAX_ROWS:
        extra = f"… {len(rows) - DIFF_MAX_ROWS} more lines"
        rows = rows[:DIFF_MAX_ROWS]
    return rows, extra


def format_edit_hunk(text: str, old: str, new: str, start: int) -> Tuple[str, str]:
    """返回 (给模型的纯文本, 给 TUI 的 markup)。"""
    rows, extra = edit_hunk_rows(text, old, new, start)
    return render_rows(rows, extra)
