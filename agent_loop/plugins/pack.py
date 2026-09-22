"""官方插件包：SKILL.md + plugin.json，安装时拷到 ~/.permanent/plugins/。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_loop.paths import spark_home

_OFFICIAL = Path(__file__).resolve().parent


def official_plugins_dir() -> Path:
    return _OFFICIAL


def official_plugin_dir(name: str) -> Path:
    return _OFFICIAL / name


def user_plugins_dir(home: Optional[Path] = None) -> Path:
    return spark_home(home) / "plugins"


def iter_official_packs() -> List[Path]:
    if not _OFFICIAL.is_dir():
        return []
    packs = []
    for child in sorted(_OFFICIAL.iterdir()):
        if child.is_dir() and (child / "plugin.json").is_file():
            packs.append(child)
    return packs


def load_manifest(pack: Path) -> Dict[str, Any]:
    path = pack / "plugin.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_factory_stub(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8")[:1200]
    except OSError:
        return False
    return "placeholder" in head.lower()


def ensure_user_plugins(home: Optional[Path] = None) -> Path:
    """缺的才拷官方包到 ~/.permanent/plugins/，不覆盖用户已改的 SKILL.md。"""
    dest_root = user_plugins_dir(home)
    dest_root.mkdir(parents=True, exist_ok=True)
    skills_root = spark_home(home) / "skills"
    for src in iter_official_packs():
        dest = dest_root / src.name
        dest_md = dest / "SKILL.md"
        if dest_md.is_file() and not _is_factory_stub(dest_md):
            continue
        legacy = skills_root / src.name / "SKILL.md"
        if not dest_md.is_file() and legacy.is_file() and not _is_factory_stub(legacy):
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(legacy.parent, dest)
            # 补上官方 plugin.json（旧 skills 目录没有）
            if (src / "plugin.json").is_file() and not (dest / "plugin.json").is_file():
                shutil.copy2(src / "plugin.json", dest / "plugin.json")
            continue
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for name in ("plugin.json", "SKILL.md"):
            piece = src / name
            if piece.is_file():
                shutil.copy2(piece, dest / name)
    return dest_root
