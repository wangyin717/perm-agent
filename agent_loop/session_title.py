"""会话显示名：chats/{id}/title.json，目录名仍是 id。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from agent_loop.paths import PathLike, _first_user_preview, session_dir, session_log_path_for

TITLE_FILE = "title.json"
AUTO_MAX = 60
MANUAL_MAX = 100
_URL_RE = re.compile(r"https?://\S+", re.I)


def title_path(
    workspace: PathLike,
    session_id: str,
    home: Optional[PathLike] = None,
) -> Path:
    return session_dir(workspace, session_id, home) / TITLE_FILE


def clip_title(text: str, limit: int = AUTO_MAX) -> str:
    stripped = _URL_RE.sub(" ", text or "")
    cleaned = " ".join(stripped.split())
    if not cleaned:
        cleaned = " ".join((text or "").split())
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def _looks_like_url_title(title: str) -> bool:
    t = (title or "").strip()
    return t.startswith("http://") or t.startswith("https://")


def read_title(
    workspace: PathLike,
    session_id: str,
    home: Optional[PathLike] = None,
) -> Dict[str, Any]:
    path = title_path(workspace, session_id, home)
    empty = {"title": "", "title_is_manual": False}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return empty
    if not isinstance(data, dict):
        return empty
    return {
        "title": str(data.get("title") or "").strip(),
        "title_is_manual": bool(data.get("title_is_manual")),
    }


def write_title(
    workspace: PathLike,
    session_id: str,
    title: str,
    *,
    manual: bool,
    home: Optional[PathLike] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    cap = MANUAL_MAX if manual else AUTO_MAX
    if limit is not None:
        cap = limit
    clipped = clip_title(title, cap)
    payload = {"title": clipped, "title_is_manual": bool(manual)}
    path = title_path(workspace, session_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def display_title(
    workspace: PathLike,
    session_id: str,
    home: Optional[PathLike] = None,
    preview: str = "",
) -> str:
    meta = read_title(workspace, session_id, home)
    return meta["title"] or preview or session_id


def ensure_auto_title(
    workspace: PathLike,
    session_id: str,
    hint: str = "",
    home: Optional[PathLike] = None,
) -> Dict[str, Any]:
    """没有标题且不是手动命名时，用 jsonl 第一条用户话写入（去掉链接）。

    自动标题若整段还是 URL，允许用去掉链接后的句子覆盖一次。
    """
    meta = read_title(workspace, session_id, home)
    if meta.get("title_is_manual"):
        return meta
    log = session_log_path_for(workspace, session_id, home)
    raw = _first_user_preview(log, 500) or hint
    seed = clip_title(raw, AUTO_MAX)
    existing = str(meta.get("title") or "")
    if existing and not _looks_like_url_title(existing):
        return meta
    if not seed:
        return meta
    if existing == seed:
        return meta
    return write_title(workspace, session_id, seed, manual=False, home=home)


def set_manual_title(
    workspace: PathLike,
    session_id: str,
    title: str,
    home: Optional[PathLike] = None,
) -> Dict[str, Any]:
    return write_title(workspace, session_id, title, manual=True, home=home, limit=MANUAL_MAX)


def reset_auto_title(
    workspace: PathLike,
    session_id: str,
    home: Optional[PathLike] = None,
) -> Dict[str, Any]:
    log = session_log_path_for(workspace, session_id, home)
    seed = _first_user_preview(log, AUTO_MAX)
    return write_title(workspace, session_id, seed, manual=False, home=home)
