"""工具执行前的确认。一定安全的直接放行；其余在 auto 下询问。"""

from __future__ import annotations

import json
import os
import shlex
from typing import Any, Dict, Optional

SAFE_TOOLS = {
    "read",
    "grep",
    "web_search",
    "web_fetch",
    "memory_search",
    "browser_screenshot",
}

SAFE_COMPUTER = {
    "list_apps",
    "list_windows",
    "get_window_state",
    "get_desktop_state",
    "get_accessibility_tree",
    "check_permissions",
    "health_report",
}

# 这类命令不记「以后同类不再问」，下次仍要确认。
BASH_NEVER_REMEMBER = {
    "rm",
    "sudo",
    "curl",
    "wget",
    "ssh",
    "scp",
    "nc",
    "ncat",
    "ftp",
}

BROWSER_SESSION_GRANT = "browser_exec:*"


def needs_approval(name: str, args: Optional[Dict[str, Any]]) -> bool:
    if name in SAFE_TOOLS:
        return False
    if name == "computer":
        action = str((args or {}).get("name") or "").strip()
        return action not in SAFE_COMPUTER
    return True


def grant_key(name: str, args: Optional[Dict[str, Any]]) -> Optional[str]:
    """同类放行的键。None 表示不能记「以后同类不再问」。"""
    args = args or {}
    if name in ("write", "edit"):
        path = os.path.normpath(str(args.get("path") or "").strip() or ".")
        return f"file:{path}"
    if name == "bash":
        word = bash_first_word(str(args.get("cmd") or ""))
        if not word or word in BASH_NEVER_REMEMBER:
            return None
        return f"bash:{word}"
    if name == "computer":
        action = str(args.get("name") or "").strip()
        app = computer_app(args.get("arguments"))
        if not action or not app:
            return None
        return f"computer:{action}:{app}"
    if name == "browser_exec":
        return None
    return None


def bash_first_word(cmd: str) -> str:
    try:
        parts = shlex.split(cmd, posix=True)
    except ValueError:
        parts = cmd.split()
    for part in parts:
        if "=" in part and not part.startswith("/") and part.split("=", 1)[0].isidentifier():
            continue
        base = os.path.basename(part)
        return base
    return ""


def computer_app(arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return ""
    if not isinstance(arguments, dict):
        return ""
    bundle = str(arguments.get("bundle_id") or "").strip()
    if bundle:
        return f"bundle:{bundle}"
    pid = arguments.get("pid")
    if pid is None and isinstance(arguments.get("target"), dict):
        pid = arguments["target"].get("pid")
    if pid is None:
        return ""
    return f"pid:{pid}"


def describe_call(name: str, args: Optional[Dict[str, Any]]) -> str:
    args = args or {}
    if name == "bash":
        return "bash  " + " ".join(str(args.get("cmd") or "").split())[:80]
    if name in ("write", "edit"):
        return f"{name}  {args.get('path') or ''}"
    if name == "computer":
        return f"computer  {args.get('name') or ''}"
    if name == "browser_exec":
        lines = str(args.get("code") or "").strip().splitlines()
        first = " ".join(lines[0].split())[:60] if lines else ""
        return f"browser_exec  {first}".rstrip()
    return name
