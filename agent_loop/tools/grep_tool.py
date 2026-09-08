"""仓库内正则搜索。失败 throw，由 ToolRuntime 写成 is_error。"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent_loop.tools.read_tool import after_tool_truncate_output, resolve_path

NAME = "grep"
REPLAY = "safe"
READ_ONLY = True

MAX_SHOWN = 50
MAX_COUNT = 10_000
MAX_FILE_BYTES = 1_000_000
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".agent",
    "dist",
    "build",
}


def before_tool_deny_empty_pattern(event: Dict[str, Any]):
    pattern = str(((event.get("args") or {}).get("pattern") or "")).strip()
    if not pattern:
        return {"block": {"reason": "pattern 为空"}}
    return None


BEFORE_HOOKS = [before_tool_deny_empty_pattern]
AFTER_HOOKS = [after_tool_truncate_output]


def which_rg() -> Optional[str]:
    return shutil.which("rg")


async def execute(args: Dict[str, Any], sandbox=None, workspace=None) -> str:
    pattern = args.get("pattern")
    if pattern is None or not str(pattern).strip():
        raise ValueError("pattern 为空")
    pattern = str(pattern)
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"bad regex: {exc}") from exc

    path = (args.get("path") or "").strip()
    glob = (args.get("glob") or "").strip() or None
    root = workspace or os.getcwd()
    target = resolve_path(path, root) if path else Path(root).resolve()
    if not target.exists():
        raise FileNotFoundError(f"path not found: {path or root}")

    display = path or "."
    rg = which_rg()
    if rg:
        try:
            shown, total, count_capped = await asyncio.to_thread(
                _rg_search, rg, pattern, target, glob, Path(root).resolve()
            )
            return _format_result(shown, total, count_capped, pattern, display)
        except FileNotFoundError:
            pass
    shown, total, count_capped = await asyncio.to_thread(
        _python_search, pattern, target, glob, Path(root).resolve()
    )
    return _format_result(shown, total, count_capped, pattern, display)


def _format_result(
    shown: List[str],
    total: int,
    count_capped: bool,
    pattern: str,
    display: str,
) -> str:
    if total == 0:
        return f'No matches for "{pattern}" in {display}'
    body = "\n".join(shown)
    if total <= MAX_SHOWN:
        word = "match" if total == 1 else "matches"
        footer = f"\n\n[Showing {total} {word}]"
    elif count_capped:
        footer = (
            f"\n\n[Showing {MAX_SHOWN} of at least {MAX_COUNT} matches. "
            "Narrow path, glob, or pattern.]"
        )
    else:
        footer = (
            f"\n\n[Showing {MAX_SHOWN} of {total} matches. "
            "Narrow path, glob, or pattern.]"
        )
    return body + footer


def _rg_search(
    rg: str,
    pattern: str,
    target: Path,
    glob: Optional[str],
    workspace: Path,
) -> Tuple[List[str], int, bool]:
    cmd = [
        rg,
        "--line-number",
        "--color=never",
        "--no-heading",
        "--with-filename",
    ]
    if glob:
        cmd.extend(["--glob", glob])
    cmd.extend(["--", pattern, str(target)])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(workspace),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("grep timed out") from exc
    if proc.returncode not in (0, 1):
        err = (proc.stderr or proc.stdout or "").strip() or f"rg exited {proc.returncode}"
        raise RuntimeError(err)
    return _collect_lines(_rel_rg_lines(proc.stdout.splitlines(), workspace))


def _rel_rg_lines(lines: Iterable[str], workspace: Path) -> Iterable[str]:
    for raw in lines:
        if not raw:
            continue
        parts = raw.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        path_s, lineno, text = parts
        display = _display_path(Path(path_s), workspace)
        yield f"{display}:{lineno}:{text}"


def _python_search(
    pattern: str,
    target: Path,
    glob: Optional[str],
    workspace: Path,
) -> Tuple[List[str], int, bool]:
    compiled = re.compile(pattern)
    shown: List[str] = []
    total = 0
    count_capped = False
    for file_path in _iter_files(target, glob):
        try:
            data = file_path.read_bytes()
        except OSError:
            continue
        if len(data) > MAX_FILE_BYTES or b"\x00" in data[:8000]:
            continue
        text = data.decode("utf-8", errors="replace")
        display = _display_path(file_path, workspace)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line) is None:
                continue
            total += 1
            if len(shown) < MAX_SHOWN:
                shown.append(f"{display}:{lineno}:{line}")
            if total >= MAX_COUNT:
                return shown, total, True
    return shown, total, count_capped


def _iter_files(target: Path, glob: Optional[str]) -> Iterable[Path]:
    if target.is_file():
        if _glob_ok(target, glob, root=target.parent):
            yield target
        return
    if not target.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        current = Path(dirpath)
        for name in filenames:
            file_path = current / name
            if _glob_ok(file_path, glob, root=target):
                yield file_path


def _glob_ok(file_path: Path, glob: Optional[str], root: Path) -> bool:
    if not glob:
        return True
    name = file_path.name
    try:
        rel = file_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = file_path.as_posix()
    return fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(rel, glob)


def _display_path(file_path: Path, workspace: Path) -> str:
    raw = Path(file_path)
    try:
        return raw.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return str(raw)


def _collect_lines(lines: Iterable[str]) -> Tuple[List[str], int, bool]:
    shown: List[str] = []
    total = 0
    for line in lines:
        total += 1
        if len(shown) < MAX_SHOWN:
            shown.append(line)
        if total >= MAX_COUNT:
            return shown, total, True
    return shown, total, False
