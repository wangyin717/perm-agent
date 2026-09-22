"""Managed install: peek at newer tags, apply perm update.

Dev checkouts (uv run perm in a git work tree) skip this. Only ~/.permanent/src
counts as a managed install.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from agent_loop.paths import spark_home

REPO_URL = "https://github.com/wangyin717/perm-agent.git"
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_PEEL = "^{}"


def src_dir(home: Optional[Path] = None) -> Path:
    return spark_home(home) / "src"


def uv_bin(home: Optional[Path] = None) -> Path:
    return spark_home(home) / "bin" / "uv"


def is_managed_install(home: Optional[Path] = None) -> bool:
    root = src_dir(home).resolve()
    if not (root / ".git").exists():
        return False
    here = Path(__file__).resolve()
    try:
        here.relative_to(root)
    except ValueError:
        return False
    return True


def parse_tag(name: str) -> Optional[Tuple[int, int, int]]:
    match = _TAG.match((name or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def newest_tag(names: Iterable[str]) -> Optional[str]:
    ranked: List[Tuple[Tuple[int, int, int], str]] = []
    for raw in names:
        parsed = parse_tag(raw)
        if parsed:
            ranked.append((parsed, raw))
    if not ranked:
        return None
    ranked.sort()
    return ranked[-1][1]


def newer_than(current: Optional[str], latest: Optional[str]) -> bool:
    a = parse_tag(current or "")
    b = parse_tag(latest or "")
    if b is None:
        return False
    if a is None:
        return True
    return b > a


def migrate_uv_tools_dir(home: Optional[Path] = None) -> Path:
    """旧目录 ~/.permanent/tools 迁到 uv-tools，避免和模型工具表撞名。"""
    root = spark_home(home)
    dest = root / "uv-tools"
    old = root / "tools"
    if not dest.exists() and old.is_dir():
        old.rename(dest)
    if dest.is_dir():
        _rewrite_uv_tools_paths(old, dest, root)
    return dest


def _rewrite_uv_tools_paths(old: Path, dest: Path, root: Path) -> None:
    old_s, new_s = str(old), str(dest)
    if old_s != new_s:
        for path in dest.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if old_s in text:
                path.write_text(text.replace(old_s, new_s), encoding="utf-8")
    bindir = root / "bin"
    if not bindir.is_dir():
        return
    for link in bindir.iterdir():
        if not link.is_symlink():
            continue
        target = os.readlink(link)
        if old_s in target:
            link.unlink()
            link.symlink_to(target.replace(old_s, new_s, 1))


def _uv_env(home: Optional[Path] = None) -> dict:
    root = spark_home(home)
    dest = migrate_uv_tools_dir(home)
    env = os.environ.copy()
    env["UV_TOOL_DIR"] = str(dest)
    env["UV_TOOL_BIN_DIR"] = str(root / "bin")
    env["UV_PYTHON_INSTALL_DIR"] = str(root / "python")
    return env


def _run(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout: float = 30,
    check: bool = False,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
        env=env,
    )


def current_tag(src: Path, timeout: float = 5) -> Optional[str]:
    try:
        proc = _run(
            ["git", "describe", "--tags", "--exact-match"],
            cwd=src,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    tag = (proc.stdout or "").strip()
    return tag if parse_tag(tag) else None


def remote_tags(repo: str = REPO_URL, timeout: float = 2.5) -> List[str]:
    try:
        proc = _run(
            ["git", "ls-remote", "--tags", "--refs", repo],
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    found: List[str] = []
    for line in (proc.stdout or "").splitlines():
        if "\t" not in line:
            continue
        _, ref = line.split("\t", 1)
        name = ref.rsplit("/", 1)[-1]
        if name.endswith(_PEEL):
            name = name[: -len(_PEEL)]
        if parse_tag(name):
            found.append(name)
    return found


def peek_update(*, home: Optional[Path] = None, timeout: float = 2.5) -> Optional[str]:
    if (os.environ.get("PERMANENT_SKIP_UPDATE_CHECK") or "").strip():
        return None
    if not is_managed_install(home):
        return None
    src = src_dir(home)
    latest = newest_tag(remote_tags(timeout=timeout))
    if not latest:
        return None
    current = current_tag(src)
    if not newer_than(current, latest):
        return None
    have = current or "untagged"
    return f"update available {have} → {latest}; run perm update"


def write_wrappers(home: Optional[Path] = None) -> None:
    root = spark_home(home)
    migrate_uv_tools_dir(home)
    perm = root / "src" / ".venv" / "bin" / "perm"
    bindir = Path.home() / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    body = (
        "#!/bin/sh\n"
        f'export PERMANENT_HOME="{root}"\n'
        f'export PATH="{root / "bin"}:$PATH"\n'
        f'export UV_TOOL_DIR="{root / "uv-tools"}"\n'
        f'export UV_TOOL_BIN_DIR="{root / "bin"}"\n'
        f'export UV_PYTHON_INSTALL_DIR="{root / "python"}"\n'
        f'exec "{perm}" "$@"\n'
    )
    for name in ("perm", "permanent"):
        path = bindir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)


def run_update(ref: Optional[str] = None, *, home: Optional[Path] = None) -> int:
    root = spark_home(home)
    src = src_dir(home)
    if not (src / ".git").is_dir():
        print(
            "not a managed install (missing ~/.permanent/src). "
            "re-run install.sh, or uv sync in a git checkout.",
            file=sys.stderr,
        )
        return 1
    uv = uv_bin(home)
    if not uv.is_file():
        print(f"uv not found at {uv}; re-run install.sh", file=sys.stderr)
        return 1
    fetch = _run(["git", "fetch", "--tags", "origin"], cwd=src, timeout=60)
    if fetch.returncode != 0:
        print(fetch.stderr or fetch.stdout or "git fetch failed", file=sys.stderr)
        return 1
    target = (ref or "").strip() or newest_tag(remote_tags(timeout=15))
    if not target:
        print("no v*.*.* tags on the remote", file=sys.stderr)
        return 1
    checkout = _run(
        ["git", "checkout", "--detach", f"refs/tags/{target}"],
        cwd=src,
        timeout=30,
    )
    if checkout.returncode != 0:
        checkout = _run(["git", "checkout", "--detach", target], cwd=src, timeout=30)
    if checkout.returncode != 0:
        print(checkout.stderr or f"cannot checkout {target}", file=sys.stderr)
        return 1
    sync = _run([str(uv), "sync"], cwd=src, timeout=180)
    if sync.returncode != 0:
        print(sync.stderr or sync.stdout or "uv sync failed", file=sys.stderr)
        return 1
    write_wrappers(root)
    from agent_loop.plugins.pack import ensure_user_plugins
    from agent_loop.prompt.skills import ensure_user_skills

    ensure_user_plugins(root)
    ensure_user_skills(root)
    _ensure_browser_use_cli(uv, root)
    print(f"updated to {target}")
    return 0


def _ensure_browser_use_cli(uv: Path, home: Optional[Path] = None) -> None:
    env = _uv_env(home)
    root = spark_home(home)
    migrate_uv_tools_dir(home)
    (root / "uv-tools").mkdir(parents=True, exist_ok=True)
    (root / "python").mkdir(parents=True, exist_ok=True)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    _run([str(uv), "python", "install", "3.12"], timeout=120, env=env)
    proc = _run(
        [str(uv), "tool", "install", "--python", "3.12", "--upgrade", "browser-use"],
        timeout=180,
        env=env,
    )
    if proc.returncode != 0:
        print(
            "browser-use CLI skipped; "
            f"{uv} tool install --python 3.12 browser-use",
            file=sys.stderr,
        )
        return
    shim = root / "bin" / "browser-use"
    link = Path.home() / ".local" / "bin" / "browser-use"
    if shim.is_file():
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(shim)
        except OSError:
            pass
