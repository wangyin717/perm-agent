"""memory/ 下的活动日志：每次 flush / dream 一行，方便看是不是 NO_REPLY。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent_loop.cli.trace import log_memory
from agent_loop.paths import project_memory_dir

ACTIVITY_NAME = "activity.jsonl"


def activity_path(workspace: str) -> Path:
    return project_memory_dir(workspace) / ACTIVITY_NAME


_QUERY_MAX = 200


def record_activity(
    workspace: str,
    kind: str,
    result: str,
    *,
    path: Optional[str] = None,
    detail: str = "",
    user_query: str = "",
    session_id: str = "",
) -> None:
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": kind,
        "result": result,
    }
    q = (user_query or "").strip()
    if q:
        row["user_query"] = q[:_QUERY_MAX]
    if session_id:
        row["session_id"] = session_id
    if path:
        row["path"] = path
    if detail:
        row["detail"] = detail
    dest = activity_path(workspace)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log_memory(
        kind,
        result,
        path=path or "",
        detail=detail,
        user_query=row.get("user_query") or "",
    )
