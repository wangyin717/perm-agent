"""从 .env 读入环境变量。已存在的 os.environ 不覆盖。"""

from __future__ import annotations

import os
from pathlib import Path

_loaded = False


def load_dotenv() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    seen = set()
    for path in _candidate_paths():
        resolved = path.resolve()
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        _apply(path)


def _candidate_paths():
    yield Path.cwd() / ".env"
    # agent_loop/envfile.py → 仓库根
    yield Path(__file__).resolve().parent.parent / ".env"


def _apply(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
