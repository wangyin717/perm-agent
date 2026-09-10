"""拼这一枪发给模型的 system：人设 yaml + 可选 AGENTS.md + cwd + 今天日期。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import yaml

_YAML = Path(__file__).resolve().parent / "system_prompt.yaml"
_AGENTS_MAX_CHARS = 8000
_persona: Optional[str] = None


def _load_persona() -> str:
    global _persona
    if _persona is None:
        data = yaml.safe_load(_YAML.read_text(encoding="utf-8"))
        _persona = str(data["system_prompt"]).strip()
    return _persona


def build_system_prompt(
    workspace: Optional[str] = None,
    *,
    today: Optional[date] = None,
) -> str:
    """workspace 是用户仓库根。today 可注入，方便测试。"""
    root = Path(workspace or ".").resolve()
    day = today or date.today()
    parts = [_load_persona()]
    agents = _read_agents_md(root)
    if agents:
        parts.append(f"# Project instructions (AGENTS.md)\n\n{agents}")
    parts.append(f"Current working directory: {root.as_posix()}")
    parts.append(f"Today's date: {day.isoformat()} ({day.strftime('%A')})")
    return "\n\n".join(parts)


def _read_agents_md(root: Path) -> str:
    path = root / "AGENTS.md"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text) > _AGENTS_MAX_CHARS:
        text = (
            text[:_AGENTS_MAX_CHARS].rstrip()
            + "\n\n[AGENTS.md truncated; read the file for the rest.]"
        )
    return text
