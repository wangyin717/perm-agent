"""Scan SKILL.md dirs and format the short list for the system prompt."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import yaml

from agent_loop.paths import spark_home
from agent_loop.plugins.pack import user_plugins_dir

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
    """安装和 perm update 时，把代码里的官方技能盖到 ~/.permanent/skills/。启动不调用。"""
    dest_root = user_skills_dir(home)
    dest_root.mkdir(parents=True, exist_ok=True)
    if not _BUNDLED.is_dir():
        return dest_root
    for child in sorted(_BUNDLED.iterdir()):
        if not child.is_dir() or not (child / "SKILL.md").is_file():
            continue
        _copy_tree_files(child, dest_root / child.name)
    return dest_root


def _copy_tree_files(src: Path, dest: Path) -> None:
    """盖掉同名文件，不删除目标里多出来的文件。"""
    dest.mkdir(parents=True, exist_ok=True)
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def skill_roots(workspace: Path) -> List[Path]:
    """技能只读 ~/.permanent/skills。插件读 ~/.permanent/plugins。

    不读代码里的 bundled_skills。官方技能要先由安装或 perm update 拷过去。
    同名时，项目里的 .permanent 目录盖过家里的。
    """
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
