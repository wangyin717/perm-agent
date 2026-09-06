"""本机执行一条 bash 命令，以及这个工具自己的 before/after hook。"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from types import SimpleNamespace
from typing import Any, Dict


def before_tool_deny_rm(event: Dict[str, Any]):
    """拦住 bash 里的 rm -rf。只会被挂在 bash 名下，不会跑到别的工具上。"""
    cmd = (event.get("args") or {}).get("cmd") or ""
    if "rm -rf" in cmd:
        return {"block": {"reason": "dangerous command"}}
    return None


def after_tool_truncate_output(event):
    content = event.get("content") or ""
    limit = 8000
    if len(content) <= limit:
        return None
    return {"content": content[:limit] + "\n...[output truncated]"}


NAME = "bash"
REPLAY = "safe"

BEFORE_HOOKS = [
    before_tool_deny_rm,
]

AFTER_HOOKS = [
    after_tool_truncate_output,
]


async def execute(args: Dict[str, Any], sandbox=None, workspace=None) -> str:
    cmd = args.get("cmd") or args.get("command") or ""
    return await run_bash(cmd, sandbox)


def _local_run(cmd: str, timeout: float = 120):
    completed = subprocess.run(
        ["bash", "-lc", cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return SimpleNamespace(
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        exit_code=completed.returncode,
    )


async def run_bash(cmd: str, sandbox=None, timeout: float = 120) -> str:
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
    if result.exit_code:
        raise RuntimeError(output or f"Command exited with code {result.exit_code}")
    return output or f"命令执行完毕（退出码 {result.exit_code}）"


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
