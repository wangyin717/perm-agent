"""拼这一枪发给模型的 system：人设 yaml + tool_notes + skill 名单 + 可选 AGENTS.md + cwd + 日期。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable, Optional

import yaml

from agent_loop.prompt.skills import discover_skills, format_skills_prompt

_YAML = Path(__file__).resolve().parent / "system_prompt.yaml"
_AGENTS_MAX_CHARS = 8000
_data: Optional[dict] = None


def _load_yaml() -> dict:
    global _data
    if _data is None:
        loaded = yaml.safe_load(_YAML.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("system_prompt.yaml must be a mapping")
        _data = loaded
    return _data


def _persona() -> str:
    return str(_load_yaml()["system_prompt"]).strip()


def _tool_notes_block(enabled: Optional[Iterable[str]] = None) -> str:
    notes = _load_yaml().get("tool_notes") or {}
    if not isinstance(notes, dict):
        return ""
    if enabled is None:
        from agent_loop.tools.registry import TOOLS

        enabled = TOOLS.keys()
    chunks = []
    for name in enabled:
        text = str(notes.get(name) or "").strip()
        if text:
            chunks.append(text)
    if not chunks:
        return ""
    return "# Tool notes\n\n" + "\n\n".join(chunks)


def build_system_prompt(
    workspace: Optional[str] = None,
    *,
    today: Optional[date] = None,
    enabled_tools: Optional[Iterable[str]] = None,
) -> str:
    """workspace 是用户仓库根。today / enabled_tools 可注入，方便测试。"""
    root = Path(workspace or ".").resolve()
    day = today or date.today()
    parts = [_persona()]
    notes = _tool_notes_block(enabled_tools)
    if notes:
        parts.append(notes)
    skills = format_skills_prompt(discover_skills(root))
    if skills:
        parts.append(skills)
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
