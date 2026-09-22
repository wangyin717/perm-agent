"""Scan SKILL.md dirs and format the short list for the system prompt."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import yaml

from agent_loop.paths import spark_home
from agent_loop.plugins.pack import (
    ensure_user_plugins,
    user_plugins_dir,
)

_BUNDLED = Path(__file__).resolve().parent.parent / "bundled_skills"
_FRONT = "---"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path


def bundled_skills_dir() -> Path:
    return _BUNDLED


def user_skills_dir(home: Optional[Path] = None) -> Path:
    return spark_home(home) / "skills"


def ensure_user_skills(home: Optional[Path] = None) -> Path:
    """Copy packaged skills into ~/.permanent/skills if that name is not there yet.

    Does not overwrite a skill the user already has.
    """
    dest_root = user_skills_dir(home)
    dest_root.mkdir(parents=True, exist_ok=True)
    if not _BUNDLED.is_dir():
        return dest_root
    for child in sorted(_BUNDLED.iterdir()):
        if not child.is_dir() or not (child / "SKILL.md").is_file():
            continue
        dest = dest_root / child.name
        dest_md = dest / "SKILL.md"
        if dest_md.is_file() and not _is_factory_stub(dest_md):
            continue
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(child, dest)
    return dest_root


def skill_roots(workspace: Path) -> List[Path]:
    """家目录 skills、官方插件、项目覆盖。同名后者赢。"""
    ensure_user_plugins()
    ensure_user_skills()
    root = Path(workspace).resolve()
    return [
        user_skills_dir(),
        user_plugins_dir(),
        root / ".permanent" / "skills",
        root / ".permanent" / "plugins",
    ]


def discover_skills(
    workspace: Path,
    *,
    roots: Optional[Iterable[Path]] = None,
) -> List[Skill]:
    found: Dict[str, Skill] = {}
    scan = list(roots) if roots is not None else skill_roots(workspace)
    for root in scan:
        root = Path(root)
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            skill_md = child / "SKILL.md" if child.is_dir() else None
            if skill_md is None or not skill_md.is_file():
                continue
            parsed = _parse_skill_md(skill_md)
            if parsed is not None:
                found[parsed.name] = parsed
    return sorted(found.values(), key=lambda s: s.name)


def format_skills_prompt(skills: List[Skill]) -> str:
    if not skills:
        return ""
    lines = [
        "# Skills",
        "When a listed skill matches the task, read its SKILL.md with the read tool "
        "before acting, then follow it. Do not guess the steps.",
        "",
    ]
    for skill in skills:
        desc = skill.description.strip() or skill.name
        lines.append(f"- {skill.name}: {desc}")
        lines.append(f"  File: {skill.path.resolve().as_posix()}")
    return "\n".join(lines).rstrip()


def _is_factory_stub(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8")[:1200]
    except OSError:
        return False
    return "placeholder" in head.lower()


def _parse_skill_md(path: Path) -> Optional[Skill]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    name = path.parent.name.strip()
    description = ""
    body = raw
    if raw.startswith(_FRONT):
        rest = raw[len(_FRONT) :]
        if rest.startswith("\n"):
            rest = rest[1:]
        end = rest.find(f"\n{_FRONT}")
        if end >= 0:
            meta_raw = rest[:end]
            body = rest[end + len(f"\n{_FRONT}") :].lstrip("\n")
            meta = yaml.safe_load(meta_raw) or {}
            if isinstance(meta, dict):
                if str(meta.get("name") or "").strip():
                    name = str(meta["name"]).strip()
                if str(meta.get("description") or "").strip():
                    description = str(meta["description"]).strip()
    if not description:
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                description = line
                break
    if not name:
        return None
    return Skill(name=name, description=description, path=path)
