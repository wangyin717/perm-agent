"""memory：短名、切块、search、flush/dream 解析。不打真实 LLM。"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent_loop.memory.activity import activity_path, record_activity
from agent_loop.memory.dream import parse_dream_text, should_dream
from agent_loop.memory.files import list_memory_files, resolve_short_path, split_chunks
from agent_loop.memory.flush import clip_flush_text, parse_flush_text, write_flush
from agent_loop.memory.slug import slugify
from agent_loop.paths import global_memory_path, project_memory_dir
from agent_loop.tools.memory_search_tool import execute
from agent_loop.tools.registry import TOOLS


def test_activity_jsonl(tmp_path):
    record_activity(str(tmp_path), "flush", "no_reply", user_query="查股价", session_id="s1")
    record_activity(str(tmp_path), "flush", "wrote", path="2026-09-10-a-s1.md", user_query="做 html")
    lines = activity_path(str(tmp_path)).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(x) for x in lines]
    assert rows[0]["result"] == "no_reply"
    assert rows[0]["user_query"] == "查股价"
    assert rows[0]["session_id"] == "s1"
    assert rows[1]["path"] == "2026-09-10-a-s1.md"


def test_registered():
    assert "memory_search" in TOOLS


def test_slugify():
    assert slugify("Hello, World!!") == "hello-world"
    assert slugify("") == "session"


def test_split_chunks_by_heading():
    text = "## Decisions\nuse pytest\n\n## Technical context\ncmd is pytest -q\n"
    chunks = split_chunks(text)
    assert [c[0] for c in chunks] == ["Decisions", "Technical context"]


def test_resolve_and_search(tmp_path):
    mem = project_memory_dir(str(tmp_path))
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text(
        "## Technical context\nThis repo uses pytest.\n", encoding="utf-8"
    )
    (mem / "2026-09-10-fetch-s1.md").write_text(
        "## Decisions\nweb_fetch saves pages to disk.\n", encoding="utf-8"
    )
    global_memory_path().write_text("## Preferences\nUser prefers concise replies.\n", encoding="utf-8")

    names = [f.short for f in list_memory_files(str(tmp_path))]
    assert names[0] == "MEMORY.md"
    assert "2026-09-10-fetch-s1.md" in names
    assert "global/MEMORY.md" in names

    out = asyncio.run(execute({"query": "pytest"}, workspace=str(tmp_path)))
    assert "[project] MEMORY.md" in out
    assert "pytest" in out.lower()
    assert "web_fetch" not in out

    one = asyncio.run(
        execute({"path": "MEMORY.md"}, workspace=str(tmp_path))
    )
    assert "[memory] MEMORY.md" in one
    assert "pytest" in one


def test_memory_get_sandbox(tmp_path):
    mem = project_memory_dir(str(tmp_path))
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text("ok\n", encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_short_path(str(tmp_path), "../chats/x.jsonl")
    with pytest.raises(ValueError):
        resolve_short_path(str(tmp_path), "/etc/passwd")


def test_clip_flush_limits_bullets_and_drops_debug():
    long_debug = "\n".join(f"- debug {i}" for i in range(6))
    text = (
        "## Decisions\n- ship it\n- keep html\n- extra three\n- extra four\n\n"
        f"## Debugging techniques\n{long_debug}\n"
    )
    clipped = clip_flush_text(text, max_chars=80, per_section=3, max_bullets=12)
    assert clipped.count("- ") <= 3
    assert "debug 0" not in clipped
    assert "ship it" in clipped


def test_parse_flush_and_append(tmp_path):
    assert parse_flush_text("NO_REPLY") is None
    assert parse_flush_text("just prose") is None
    body = "## Decisions\n- ship it\n"
    parsed = parse_flush_text(body)
    assert parsed is not None
    assert "ship it" in parsed
    dest = tmp_path / "note.md"
    write_flush(dest, body)
    write_flush(dest, "## Technical context\npytest\n")
    text = dest.read_text(encoding="utf-8")
    assert text.count("---") == 1
    assert "flush" in text


def test_parse_dream_and_gates(tmp_path, monkeypatch):
    assert parse_dream_text("NO_REPLY") is None
    assert parse_dream_text("## Decisions\nkeep\n")
    monkeypatch.setenv("SPARK_AGENT_DREAM_MIN_FILES", "2")
    monkeypatch.setenv("SPARK_AGENT_DREAM_MIN_HOURS", "0")
    mem = project_memory_dir(str(tmp_path))
    mem.mkdir(parents=True, exist_ok=True)
    assert should_dream(str(tmp_path)) is False
    (mem / "2026-09-10-a-s1.md").write_text("## A\nx\n", encoding="utf-8")
    (mem / "2026-09-11-b-s2.md").write_text("## B\ny\n", encoding="utf-8")
    assert should_dream(str(tmp_path)) is True
