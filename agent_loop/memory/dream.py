"""空闲/过门后把中期收成项目 MEMORY.md。一次模型，覆盖写。"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from agent_loop.memory.activity import record_activity
from agent_loop.memory.files import list_memory_files
from agent_loop.paths import project_memory_dir

DREAM_MAX_TOKENS = 2000
DREAM_MAX_CHARS = 8000
STATE_NAME = ".dream.json"

DREAM_SYSTEM = (
    "You maintain a project's long-term MEMORY.md for a coding agent. "
    "Do NOT call tools. If there is nothing durable to keep, reply NO_REPLY."
)

DREAM_USER = """Merge the existing project MEMORY.md with recent session notes into ONE replacement MEMORY.md.

Rules:
- Integrate related facts; do not append a diary.
- Newer facts override older ones. Keep only what is still true.
- Convert relative time ("yesterday") to absolute dates.
- Drop greetings, tool noise, Current state, Next steps, and user OS/editor/language prefs (those live in global memory).
- Keep decisions, architecture, and problem/solution notes.
- Hard cap: {max_chars} characters. If over, drop the least reusable bullets.
- Use ## headings.
- If nothing belongs in long-term memory, reply NO_REPLY.

<existing_memory>
{existing}
</existing_memory>

<session_notes>
{notes}
</session_notes>"""


def _state_path(workspace: str) -> Path:
    return project_memory_dir(workspace) / STATE_NAME


def _load_state(workspace: str) -> dict:
    path = _state_path(workspace)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(workspace: str, **fields: Any) -> None:
    path = _state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_state(workspace)
    data.update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _min_hours() -> float:
    raw = (os.environ.get("SPARK_AGENT_DREAM_MIN_HOURS") or "12").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 12.0


def _min_files() -> int:
    raw = (os.environ.get("SPARK_AGENT_DREAM_MIN_FILES") or "2").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


def _medium_files(workspace: str) -> List[Path]:
    return [f.path for f in list_memory_files(workspace) if f.source == "session"]


def should_dream(workspace: str) -> bool:
    files = _medium_files(workspace)
    if len(files) < _min_files():
        return False
    state = _load_state(workspace)
    last = float(state.get("last_unix") or 0)
    if last and (time.time() - last) < _min_hours() * 3600:
        return False
    if last:
        newer = [p for p in files if p.stat().st_mtime > last]
        if len(newer) < _min_files():
            return False
    return True


def parse_dream_text(text: str) -> Optional[str]:
    body = (text or "").strip()
    if not body:
        return None
    if body.upper() == "NO_REPLY" or body.upper().startswith("NO_REPLY"):
        return None
    if "##" not in body:
        return None
    if len(body) > DREAM_MAX_CHARS:
        body = body[:DREAM_MAX_CHARS].rstrip() + "\n"
    return body


async def maybe_dream(
    llm: Any,
    workspace: str,
    abort: Any = None,
    user_query: str = "",
    session_id: str = "",
) -> Optional[Path]:
    if not should_dream(workspace):
        logging.info("[memory:dream] gates closed")
        return None
    mem_dir = project_memory_dir(workspace)
    existing_path = mem_dir / "MEMORY.md"
    existing = ""
    if existing_path.is_file():
        existing = existing_path.read_text(encoding="utf-8")
    notes = []
    for path in _medium_files(workspace)[:12]:
        try:
            notes.append(f"# {path.name}\n{path.read_text(encoding='utf-8')}")
        except OSError:
            continue
    blob = "\n\n---\n\n".join(notes)
    messages = [
        {"role": "system", "content": DREAM_SYSTEM},
        {
            "role": "user",
            "content": DREAM_USER.format(
                max_chars=DREAM_MAX_CHARS, existing=existing or "(empty)", notes=blob or "(none)"
            ),
        },
    ]
    try:
        response = await llm.call(messages, tools=None, abort=abort, max_tokens=DREAM_MAX_TOKENS)
    except Exception as exc:
        logging.warning("[memory:dream] skipped: %s", exc)
        record_activity(
            workspace, "dream", "error", detail=str(exc)[:200],
            user_query=user_query, session_id=session_id,
        )
        return None
    body = parse_dream_text(getattr(response, "text", "") or "")
    if not body:
        _save_state(workspace, last_unix=time.time(), last_iso=datetime.now(timezone.utc).isoformat())
        record_activity(
            workspace, "dream", "no_reply",
            user_query=user_query, session_id=session_id,
        )
        return None
    dest = mem_dir / "MEMORY.md"
    dest.write_text(body.rstrip() + "\n", encoding="utf-8")
    _save_state(workspace, last_unix=time.time(), last_iso=datetime.now(timezone.utc).isoformat())
    record_activity(
        workspace, "dream", "wrote", path=dest.name,
        user_query=user_query, session_id=session_id,
    )
    return dest
