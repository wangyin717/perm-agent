"""仓库内正则搜索。失败 throw，由 ToolRuntime 写成 is_error。"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent_loop.tools.read_tool import after_tool_truncate_output, resolve_path

NAME = "grep"
REPLAY = "safe"
READ_ONLY = True

PAGE_SIZE = 20
MAX_SHOWN = PAGE_SIZE  # 兼容旧测试名
MAX_COUNT = 10_000
MAX_FILE_BYTES = 1_000_000
_CURSOR_RE = re.compile(r"^g1\.([0-9a-f]{12})\.(\d+)$")
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
    args = event.get("args") or {}
    pattern = str(args.get("pattern") or "").strip()
    cursor = str(args.get("cursor") or "").strip()
    if not pattern and not cursor:
        return {"block": {"reason": "pattern 为空"}}
    return None


BEFORE_HOOKS = [before_tool_deny_empty_pattern]
AFTER_HOOKS = [after_tool_truncate_output]


def which_rg() -> Optional[str]:
    return shutil.which("rg")


async def execute(
    args: Dict[str, Any], sandbox=None, workspace=None, session_dir=None
) -> str:
    cursor = str(args.get("cursor") or "").strip()
    if cursor:
        return _page_from_cursor(cursor, args, session_dir)

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
    hits, count_capped = await _run_search(pattern, target, glob, Path(root).resolve())
    short_id = None
    if session_dir and hits:
        short_id = uuid.uuid4().hex[:12]
        _write_snapshot(session_dir, short_id, pattern, path, glob, display, hits, count_capped)
    return _format_page(
        hits,
        offset=0,
        short_id=short_id,
        pattern=pattern,
        display=display,
        count_capped=count_capped,
    )


async def _run_search(
    pattern: str, target: Path, glob: Optional[str], workspace: Path
) -> Tuple[List[str], bool]:
    rg = which_rg()
    if rg:
        try:
            return await asyncio.to_thread(_rg_search, rg, pattern, target, glob, workspace)
        except FileNotFoundError:
            pass
    return await asyncio.to_thread(_python_search, pattern, target, glob, workspace)


def _grep_file(session_dir: str, short_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", short_id):
        raise ValueError("bad cursor")
    return Path(session_dir) / "workspace" / "tools_result" / "grep" / short_id


def _write_snapshot(
    session_dir: str,
    short_id: str,
    pattern: str,
    path: str,
    glob: Optional[str],
    display: str,
    hits: List[str],
    count_capped: bool,
) -> None:
    dest = _grep_file(session_dir, short_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "v": 1,
        "pattern": pattern,
        "path": path,
        "glob": glob,
        "display": display,
        "count_capped": count_capped,
    }
    dest.write_text(
        json.dumps(meta, ensure_ascii=False) + "\n" + "\n".join(hits) + ("\n" if hits else ""),
        encoding="utf-8",
    )


def _read_snapshot(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    raw = path.read_text(encoding="utf-8")
    first, _, rest = raw.partition("\n")
    try:
        meta = json.loads(first)
    except json.JSONDecodeError as exc:
        raise ValueError("bad grep snapshot") from exc
    if not isinstance(meta, dict):
        raise ValueError("bad grep snapshot")
    hits = [line for line in rest.splitlines() if line]
    return meta, hits


def _parse_cursor(raw: str) -> Tuple[str, int]:
    matched = _CURSOR_RE.match(raw.strip())
    if not matched:
        raise ValueError("bad cursor")
    return matched.group(1), int(matched.group(2))


def _page_from_cursor(cursor: str, args: Dict[str, Any], session_dir: Optional[str]) -> str:
    if not session_dir:
        raise ValueError("cursor requires a session directory")
    short_id, offset = _parse_cursor(cursor)
    path = _grep_file(session_dir, short_id)
    if not path.is_file():
        raise FileNotFoundError(f"grep snapshot not found: {short_id}")
    meta, hits = _read_snapshot(path)
    extra_pattern = str(args.get("pattern") or "").strip()
    stored_pattern = str(meta.get("pattern") or "")
    if extra_pattern and extra_pattern != stored_pattern:
        raise ValueError("cursor does not match pattern")
    return _format_page(
        hits,
        offset=offset,
        short_id=short_id,
        pattern=stored_pattern,
        display=str(meta.get("display") or "."),
        count_capped=bool(meta.get("count_capped")),
    )


def _format_page(
    hits: List[str],
    offset: int,
    short_id: Optional[str],
    pattern: str,
    display: str,
    count_capped: bool,
) -> str:
    total = len(hits)
    if total == 0:
        return f'No matches for "{pattern}" in {display}'
    if offset > total:
        raise ValueError("cursor past the end")
    page = hits[offset : offset + PAGE_SIZE]
    if offset > 0 and not page:
        raise ValueError("cursor past the end")
    body = "\n".join(page)
    shown = len(page)
    next_offset = offset + shown
    has_more = next_offset < total
    cursor = f"g1.{short_id}.{next_offset}" if short_id and has_more else None
    total_label = f"at least {MAX_COUNT}" if count_capped else str(total)

    if total <= PAGE_SIZE and offset == 0 and not count_capped:
        word = "match" if total == 1 else "matches"
        footer = f"\n\n[Showing {total} {word}]"
    elif has_more and cursor:
        footer = f'\n\n[Showing {shown} of {total_label} matches. Continue with cursor="{cursor}".]'
    elif has_more:
        footer = (
            f"\n\n[Showing {shown} of {total_label} matches. "
            "Narrow path, glob, or pattern.]"
        )
    elif count_capped:
        footer = (
            f"\n\n[Showing {shown} of at least {MAX_COUNT} matches. "
            "Narrow path, glob, or pattern.]"
        )
    else:
        footer = f"\n\n[Showing {shown} of {total} matches]"
    return body + footer


def _rg_search(
    rg: str,
    pattern: str,
    target: Path,
    glob: Optional[str],
    workspace: Path,
) -> Tuple[List[str], bool]:
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
    return _collect_hits(_rel_rg_lines(proc.stdout.splitlines(), workspace))


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
) -> Tuple[List[str], bool]:
    compiled = re.compile(pattern)
    hits: List[str] = []
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
            hits.append(f"{display}:{lineno}:{line}")
            if len(hits) >= MAX_COUNT:
                return hits, True
    return hits, False


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


def _collect_hits(lines: Iterable[str]) -> Tuple[List[str], bool]:
    hits: List[str] = []
    for line in lines:
        hits.append(line)
        if len(hits) >= MAX_COUNT:
            return hits, True
    return hits, False
