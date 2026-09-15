"""给 TUI / 模型看的短 hunk。"""

from __future__ import annotations

import difflib
import re
from typing import List, Optional, Tuple

DIFF_CTX = 2
DIFF_MAX_ROWS = 400
DIFF_LINE_CHARS = 160

_AT = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)")


def clip_diff_line(text: str) -> str:
    if len(text) <= DIFF_LINE_CHARS:
        return text
    return text[: DIFF_LINE_CHARS - 1] + "…"


def plain_row(kind: str, ln: int, body: str) -> str:
    sign = {"del": "-", "add": "+", "ctx": " "}[kind]
    return f"{sign}{ln:>4}|{clip_diff_line(body)}"


def mark_row(kind: str, ln: int, body: str) -> str:
    body = clip_diff_line(body).replace("[", "\\[")
    num = f"{ln:>4}"
    if kind == "del":
        return f"[#cd3048 on #f5dade]-{num}|{body}[/]"
    if kind == "add":
        return f"[#378e23 on #daf2dc]+{num}|{body}[/]"
    return f"[#6e6e73] {num}|[/]{body}"


def render_rows(rows: List[Tuple[str, int, str]], extra: str = "") -> Tuple[str, str]:
    if len(rows) > DIFF_MAX_ROWS:
        omitted = len(rows) - DIFF_MAX_ROWS
        rows = rows[:DIFF_MAX_ROWS]
        extra = extra or f"… {omitted} more lines"
    plain = "\n".join(plain_row(k, n, b) for k, n, b in rows)
    marked = "\n".join(mark_row(k, n, b) for k, n, b in rows)
    if extra:
        if plain:
            plain = f"{plain}\n{extra}"
        else:
            plain = extra
        marked = f"{marked}\n[#6e6e73]{extra}[/]" if marked else f"[#6e6e73]{extra}[/]"
    return plain, marked


def pack_rows(rows: List[Tuple[str, int, str]]) -> List[dict]:
    return [{"kind": k, "ln": n, "body": b} for k, n, b in rows]


def write_hunk_rows(old: Optional[str], new: str) -> Tuple[List[Tuple[str, int, str]], str]:
    """新建：前几行全是 +。覆盖：unified diff。"""
    new_lines = new.splitlines()
    if old is None:
        extra = ""
        if len(new_lines) > DIFF_MAX_ROWS:
            extra = f"… {len(new_lines) - DIFF_MAX_ROWS} more lines"
        rows = [("add", i + 1, ln) for i, ln in enumerate(new_lines[:DIFF_MAX_ROWS])]
        if not rows and new == "":
            return [], "empty file"
        return rows, extra
    old_lines = old.splitlines()
    if old_lines == new_lines:
        return [], ""
    rows: List[Tuple[str, int, str]] = []
    old_ln = new_ln = 0
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=DIFF_CTX):
        if line.startswith("---") or line.startswith("+++"):
            continue
        at = _AT.match(line)
        if at:
            old_ln = int(at.group(1)) - 1
            new_ln = int(at.group(2)) - 1
            continue
        if line.startswith("-"):
            old_ln += 1
            rows.append(("del", old_ln, line[1:]))
        elif line.startswith("+"):
            new_ln += 1
            rows.append(("add", new_ln, line[1:]))
        elif line.startswith(" "):
            old_ln += 1
            new_ln += 1
            rows.append(("ctx", new_ln, line[1:]))
    extra = ""
    if len(rows) > DIFF_MAX_ROWS:
        extra = f"… {len(rows) - DIFF_MAX_ROWS} more lines"
        rows = rows[:DIFF_MAX_ROWS]
    return rows, extra


def format_write_hunk(old: Optional[str], new: str) -> Tuple[str, str]:
    rows, extra = write_hunk_rows(old, new)
    return render_rows(rows, extra)
