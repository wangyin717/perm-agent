"""本机执行一条 bash 命令，以及这个工具自己的 before hook。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from agent_loop.paths import spark_home

_GUI_IN_CMD = re.compile(
    r"\btkinter\.Tk\s*\(|\btk\.Tk\s*\(|\bTk\s*\(|pygame\.display",
    re.I,
)
_PY_SCRIPT = re.compile(
    r"(?:^|[;&\n]|&&|\|\|)\s*(?:\S*python3(?:\.\d+)?)\s+[\"']?(\S+\.py)",
    re.I,
)
_GUI_IN_FILE = re.compile(
    r"\btkinter\.Tk\s*\(|\btk\.Tk\s*\(|pygame\.display\.set_mode",
    re.I,
)


def _bash_text(event: Dict[str, Any]) -> str:
    args = event.get("args") or {}
    return str(args.get("cmd") or args.get("command") or "")


def before_tool_deny_rm(event: Dict[str, Any]):
    """拦住 bash 里的 rm -rf。只会被挂在 bash 名下，不会跑到别的工具上。"""
    if "rm -rf" in _bash_text(event):
        return {"block": {"reason": "dangerous command"}}
    return None


def before_tool_deny_gui(event: Dict[str, Any]):
    """系统 python3 + Tk 会 Abort trap 6，弹崩溃窗。GUI 不要在 bash 里开。"""
    cmd = _bash_text(event)
    if _GUI_IN_CMD.search(cmd):
        return {
            "block": {
                "reason": "Do not open a GUI from bash (tk.Tk / pygame). "
                "System python3 Tk aborts on this Mac (Abort trap 6). "
                "Use a headless unit test instead."
            }
        }
    for match in _PY_SCRIPT.finditer(cmd):
        path = match.group(1).strip("\"'")
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            continue
        if _GUI_IN_FILE.search(text):
            return {
                "block": {
                    "reason": f"Do not run {path} from bash: it creates a GUI window. "
                    "That crashes system python3 (Abort trap 6). "
                    "Use a headless test, or tell the user to run it themselves."
                }
            }
    return None


NAME = "bash"
REPLAY = "safe"
READ_ONLY = False

BEFORE_HOOKS = [
    before_tool_deny_rm,
    before_tool_deny_gui,
]
AFTER_HOOKS: list = []

MAX_LINES = 2000
MAX_BYTES = 50 * 1024
HEAD_LINES = 200
TAIL_LINES = 1800


async def execute(args: Dict[str, Any], sandbox=None, workspace=None, session_dir=None) -> str:
    cmd = args.get("cmd") or args.get("command") or ""
    return await run_bash(cmd, sandbox, session_dir=session_dir, workspace=workspace)


def _bash_env() -> dict:
    env = os.environ.copy()
    home_bin = str(spark_home() / "bin")
    env["PATH"] = home_bin + os.pathsep + env.get("PATH", "")
    return env


def _local_run(cmd: str, timeout: float = 120):
    completed = subprocess.run(
        ["bash", "-lc", cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_bash_env(),
    )
    return SimpleNamespace(
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        exit_code=completed.returncode,
    )


async def run_bash(
    cmd: str,
    sandbox=None,
    timeout: float = 120,
    session_dir: Optional[str] = None,
    workspace: Optional[str] = None,
) -> str:
    """失败必须 throw。默认本机 bash；测试可传入带 commands.run 的 sandbox。"""
    cmd = (cmd or "").strip()
    if not cmd:
        raise ValueError("cmd 为空")
    try:
        result = await _run_once(sandbox, cmd, timeout)
    except RuntimeError as exc:
        if "timed out" not in str(exc):
            raise
        logging.warning("[bash] 超时，用同一命令重试 1 次")
        result = await _run_once(sandbox, cmd, timeout)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if not output and not result.exit_code:
        return f"命令执行完毕（退出码 {result.exit_code}）"
    shown = _preview_and_spill(output or "", session_dir, workspace)
    if result.exit_code:
        raise RuntimeError(shown or f"Command exited with code {result.exit_code}")
    return shown


def _preview_and_spill(
    full: str, session_dir: Optional[str], workspace: Optional[str]
) -> str:
    lines = full.splitlines()
    n_lines = len(lines)
    n_bytes = len(full.encode("utf-8"))
    if n_lines <= MAX_LINES and n_bytes <= MAX_BYTES:
        return full
    saved = ""
    if session_dir:
        saved = _write_output(session_dir, workspace, full)
    preview = _head_tail(lines)
    footer = _footer(saved, n_bytes, n_lines, bool(session_dir))
    return preview + footer


def _head_tail(lines: List[str]) -> str:
    n = len(lines)
    head_n = min(HEAD_LINES, n)
    tail_n = min(TAIL_LINES, max(0, n - head_n))
    tail_start = n - tail_n
    omitted = max(0, tail_start - head_n)
    head = list(lines[:head_n])
    tail = list(lines[tail_start:]) if tail_n else []

    def render() -> str:
        parts = list(head)
        if omitted:
            parts.append(f"... {omitted} lines omitted ...")
        parts.extend(tail)
        return "\n".join(parts)

    text = render()
    while head and len(text.encode("utf-8")) > MAX_BYTES:
        head.pop()
        omitted += 1
        text = render()
    while tail and len(text.encode("utf-8")) > MAX_BYTES:
        tail.pop(0)
        omitted += 1
        text = render()
    if len(text.encode("utf-8")) > MAX_BYTES:
        chunk = text.encode("utf-8")[-MAX_BYTES:]
        return chunk.decode("utf-8", errors="ignore")
    return text


def _footer(saved: str, n_bytes: int, n_lines: int, had_session: bool) -> str:
    if saved:
        return (
            f"\n\nsaved: {saved}\n"
            f"chars: {n_bytes}\n"
            f"lines: {n_lines}\n\n"
            "To find something, grep or read the saved path "
            "(pass path= that file). Do not pipe head/tail."
        )
    if had_session:
        return "\n\n[output truncated; failed to save session file]"
    return (
        f"\n\n[showing head/tail of {n_lines} lines / {n_bytes} bytes; "
        "full output was not saved (no session directory)]"
    )


def _write_output(session_dir: str, workspace: Optional[str], full: str) -> str:
    short_id = uuid.uuid4().hex[:12]
    dest = Path(session_dir) / "workspace" / "tools_result" / "bash" / f"{short_id}.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(full, encoding="utf-8")
    root = Path(workspace or ".").resolve()
    try:
        return dest.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(dest)


async def _run_once(sandbox, cmd: str, timeout: float):
    runner = sandbox.commands.run if sandbox is not None else _local_run
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(runner, cmd, timeout=timeout),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"Command timed out after {timeout} seconds") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out after {timeout} seconds") from None
    except Exception as exc:
        name = type(exc).__name__.lower()
        if "timeout" in name or "timed out" in str(exc).lower():
            raise RuntimeError(f"Command timed out after {timeout} seconds") from exc
        raise
