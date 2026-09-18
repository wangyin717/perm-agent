"""回合停稳后写中期 md。另打一枪模型；NO_REPLY 则不落盘。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_loop.memory.activity import record_activity
from agent_loop.memory.slug import slugify
from agent_loop.paths import project_memory_dir
from agent_loop.session_log import SessionLog

FLUSH_MAX_TOKENS = 400
FLUSH_TRANSCRIPT_CHARS = 12000
FLUSH_WRITE_MAX_CHARS = 2000
FLUSH_MAX_BULLETS_PER_SECTION = 3
FLUSH_MAX_BULLETS_TOTAL = 12

FLUSH_SYSTEM = (
    "You extract durable notes for a coding agent's long-term memory. "
    "Do NOT continue the conversation or call tools. "
    "If this turn is routine Q&A with no new decisions or reusable facts, "
    "reply with exactly NO_REPLY and nothing else."
)

FLUSH_USER = """Extract reusable facts from this coding-agent session. Write markdown that WILL be stored for future sessions.

Rules:
- If there is nothing durable (chit-chat, one-off lookups, no decisions), reply NO_REPLY.
- Keep it short: at most 12 bullets total, at most 3 bullets per ## section, aim under 1500 characters.
- Omit any ## section that has nothing to say. Do NOT fill all four headings for completeness.
- Only keep: paths, commands, decisions, and root causes of bugs. Skip UI inventories, widget lists, and one-off verification steps (how you tested this turn).
- Do NOT write Current state or Next steps (that belongs in the in-session summary).
- Do NOT write user preferences like OS, shell, editor, or language (those belong in global memory).
- Do NOT narrate tool calls.
- Use ## headings when relevant: Decisions & rationale; Technical context; Debugging techniques; Problems & solutions.
- Prefer workspace-relative file paths.
- Every section must start with ##. If you cannot produce any ## heading, reply NO_REPLY.

<transcript>
{transcript}
</transcript>"""


def first_user_text(rows: List[Dict[str, Any]]) -> str:
    for row in rows:
        if row.get("kind") == "entry" and row.get("type") == "user":
            return str(row.get("content") or "")
    return ""


def session_transcript(rows: List[Dict[str, Any]], max_chars: int = FLUSH_TRANSCRIPT_CHARS) -> str:
    parts = []
    for row in rows:
        if row.get("kind") != "entry":
            continue
        kind = row.get("type")
        if kind == "user":
            parts.append("User: " + str(row.get("content") or ""))
        elif kind == "assistant":
            text = str(row.get("content") or "").strip()
            if text:
                parts.append("Assistant: " + text)
    blob = "\n\n".join(parts)
    if len(blob) > max_chars:
        blob = blob[-max_chars:]
    return blob


def parse_flush_text(text: str) -> Optional[str]:
    body = (text or "").strip()
    if not body:
        return None
    if body.upper() == "NO_REPLY" or body.upper().startswith("NO_REPLY"):
        return None
    if "##" not in body:
        return None
    clipped = clip_flush_text(body)
    return clipped or None


def clip_flush_text(
    text: str,
    *,
    max_chars: int = FLUSH_WRITE_MAX_CHARS,
    per_section: int = FLUSH_MAX_BULLETS_PER_SECTION,
    max_bullets: int = FLUSH_MAX_BULLETS_TOTAL,
) -> str:
    """每节最多 3 条、全文最多 12 条；超长先丢掉 Debugging，再截断。"""
    parts = []
    current_heading = None
    current_lines: List[str] = []

    def flush_section() -> None:
        if not current_heading:
            return
        bullets = [ln for ln in current_lines if ln.lstrip().startswith(("-", "*"))]
        if not bullets:
            bullets = [ln for ln in current_lines if ln.strip()]
        if not bullets:
            return
        parts.append((current_heading, bullets[:per_section]))

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("##"):
            flush_section()
            current_heading = line
            current_lines = []
        elif current_heading is not None:
            current_lines.append(line)
    flush_section()

    kept: List[tuple] = []
    total = 0
    for heading, bullets in parts:
        room = max_bullets - total
        if room <= 0:
            break
        take = bullets[:room]
        kept.append((heading, take))
        total += len(take)

    def render(sections: List[tuple]) -> str:
        blocks = []
        for heading, bullets in sections:
            blocks.append(heading.strip() + "\n\n" + "\n".join(bullets))
        return "\n\n".join(blocks).strip()

    body = render(kept)
    if len(body) <= max_chars:
        return body
    dropped = [
        (h, b)
        for h, b in kept
        if "debug" not in h.lower()
    ]
    body = render(dropped) or body
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars].rsplit("\n", 1)[0].rstrip()
    return cut


def medium_path(workspace: str, session_id: str, first_user: str, now: Optional[datetime] = None) -> Path:
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    slug = slugify(first_user)
    sid = slugify(session_id, max_len=40)
    return project_memory_dir(workspace) / f"{day}-{slug}-{sid}.md"


def write_flush(path: Path, body: str, now: Optional[datetime] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d %H:%M")
    block = f"<!-- flush {stamp} -->\n\n" + body.rstrip() + "\n"
    if path.is_file() and path.stat().st_size > 0:
        block = "\n---\n" + block
    with open(path, "a", encoding="utf-8") as f:
        f.write(block)


async def maybe_flush(
    log: SessionLog,
    llm: Any,
    workspace: str,
    session_id: str,
    abort: Any = None,
    user_query: str = "",
) -> Optional[Path]:
    rows = log.read_all()
    transcript = session_transcript(rows)
    if not transcript.strip():
        record_activity(
            workspace, "flush", "skipped", detail="empty transcript",
            user_query=user_query, session_id=session_id,
        )
        return None
    messages = [
        {"role": "system", "content": FLUSH_SYSTEM},
        {"role": "user", "content": FLUSH_USER.format(transcript=transcript)},
    ]
    try:
        response = await llm.call(messages, tools=None, abort=abort, max_tokens=FLUSH_MAX_TOKENS)
    except Exception as exc:
        logging.warning("[memory:flush] skipped: %s", exc)
        record_activity(
            workspace, "flush", "error", detail=str(exc)[:200],
            user_query=user_query, session_id=session_id,
        )
        return None
    body = parse_flush_text(getattr(response, "text", "") or "")
    if not body:
        record_activity(
            workspace, "flush", "no_reply",
            user_query=user_query, session_id=session_id,
        )
        return None
    dest = medium_path(workspace, session_id, first_user_text(rows))
    write_flush(dest, body)
    record_activity(
        workspace, "flush", "wrote", path=dest.name,
        user_query=user_query, session_id=session_id,
    )
    return dest
