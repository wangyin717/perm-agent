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


def _run(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout: float = 30,
    check: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
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
    perm = root / "src" / ".venv" / "bin" / "perm"
    bindir = Path.home() / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    body = (
        "#!/bin/sh\n"
        f'export PERMANENT_HOME="{root}"\n'
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
    print(f"updated to {target}")
    return 0
