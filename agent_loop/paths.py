"""家目录布局：会话、快照、记忆都在 ~/.spark-agent，不写进用户仓库。

~/.spark-agent/MEMORY.md
~/.spark-agent/projects/{slug}-{hash}/
  memory/MEMORY.md
  memory/YYYY-MM-DD-slug-{sessionId}.md
  chats/{sessionId}/session.jsonl
  chats/{sessionId}/workspace/tools_result/…
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

PathLike = Union[str, Path]


def spark_home(override: Optional[PathLike] = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    env = (os.environ.get("SPARK_AGENT_HOME") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".spark-agent"


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-._").lower()
    return (cleaned or "project")[:40]


def project_key(workspace: PathLike) -> str:
    root = Path(workspace).expanduser().resolve()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{_slug(root.name)}-{digest}"


def project_dir(workspace: PathLike, home: Optional[PathLike] = None) -> Path:
    return spark_home(home) / "projects" / project_key(workspace)


def session_dir(workspace: PathLike, session_id: str, home: Optional[PathLike] = None) -> Path:
    sid = session_id.strip() or "default"
    return project_dir(workspace, home) / "chats" / sid


def session_log_path_for(
    workspace: PathLike,
    session_id: str,
    home: Optional[PathLike] = None,
) -> Path:
    return session_dir(workspace, session_id, home) / "session.jsonl"


def session_log_path(user_action_data: Dict[str, Any]) -> Path:
    session_id = str(user_action_data.get("sessionId") or "default")
    workspace = user_action_data.get("workspace") or os.getcwd()
    home = user_action_data.get("sparkHome")
    return session_log_path_for(workspace, session_id, home)


def global_memory_path(home: Optional[PathLike] = None) -> Path:
    return spark_home(home) / "MEMORY.md"


def project_memory_dir(workspace: PathLike, home: Optional[PathLike] = None) -> Path:
    return project_dir(workspace, home) / "memory"


def ensure_project_layout(workspace: PathLike, home: Optional[PathLike] = None) -> Path:
    """建好 projects/{key}/memory 和 chats 的父目录。不写 MEMORY.md。"""
    root = spark_home(home)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    proj = project_dir(workspace, home)
    (proj / "memory").mkdir(parents=True, exist_ok=True)
    (proj / "chats").mkdir(parents=True, exist_ok=True)
    return proj
