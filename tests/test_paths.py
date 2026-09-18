"""家目录项目路径。"""
from __future__ import annotations

import json
from pathlib import Path

from agent_loop.paths import list_sessions, project_dir, project_key, session_log_path_for, spark_home
from agent_loop.session_log import session_log_path


def test_project_key_stable_and_safe(tmp_path):
    key = project_key(tmp_path)
    assert key == project_key(tmp_path / ".")
    assert "-" in key
    assert project_key(tmp_path) == project_key(str(tmp_path))


def test_session_log_path_layout(tmp_path):
    home = Path(spark_home())
    path = session_log_path({"sessionId": "wy1", "workspace": str(tmp_path)})
    proj = project_dir(tmp_path)
    assert path == proj / "chats" / "wy1" / "session.jsonl"
    assert (proj / "memory").is_dir()
    assert (proj / "chats").is_dir()
    path.resolve().relative_to(home.resolve())
    assert ".agent" not in path.parts


def test_list_sessions_newest_first(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SPARK_AGENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    older = session_log_path_for(ws, "old-a", home=home)
    newer = session_log_path_for(ws, "new-b", home=home)
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    older.write_text(
        '{"kind":"entry","type":"user","content":"first question here"}\n',
        encoding="utf-8",
    )
    newer.write_text(
        '{"kind":"entry","type":"user","content":"second"}\n',
        encoding="utf-8",
    )
    import os
    import time

    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))
    items = list_sessions(ws, home=home)
    assert [row["id"] for row in items] == ["new-b", "old-a"]
    assert items[1]["preview"] == "first question here"


def test_session_title_sidecar(tmp_path, monkeypatch):
    from agent_loop.session_title import (
        ensure_auto_title,
        read_title,
        reset_auto_title,
        set_manual_title,
        title_path,
    )

    home = tmp_path / "home"
    monkeypatch.setenv("SPARK_AGENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    log = session_log_path_for(ws, "s1", home=home)
    log.parent.mkdir(parents=True)
    log.write_text(
        '{"kind":"entry","type":"user","content":"first question here"}\n',
        encoding="utf-8",
    )
    meta = ensure_auto_title(ws, "s1", "ignored later", home=home)
    assert meta["title"] == "first question here"
    assert meta["title_is_manual"] is False
    assert title_path(ws, "s1", home=home).is_file()
    again = ensure_auto_title(ws, "s1", "should not replace", home=home)
    assert again["title"] == "first question here"
    set_manual_title(ws, "s1", "My Name", home=home)
    assert read_title(ws, "s1", home=home)["title_is_manual"] is True
    ensure_auto_title(ws, "s1", "nope", home=home)
    assert read_title(ws, "s1", home=home)["title"] == "My Name"
    reset_auto_title(ws, "s1", home=home)
    assert read_title(ws, "s1", home=home) == {
        "title": "first question here",
        "title_is_manual": False,
    }
    items = list_sessions(ws, home=home)
    assert items[0]["title"] == "first question here"


def test_auto_title_strips_leading_url(tmp_path, monkeypatch):
    from agent_loop.session_title import clip_title, ensure_auto_title, read_title, write_title

    home = tmp_path / "home"
    monkeypatch.setenv("SPARK_AGENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    sid = "url-s"
    log = session_log_path_for(ws, sid, home=home)
    log.parent.mkdir(parents=True)
    query = "https://grok.com/share/abc123 看下这段对话 你觉得放到agent里面怎么用呢"
    log.write_text(
        json.dumps({"kind": "entry", "type": "user", "content": query}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    assert clip_title(query).startswith("看下这段对话")
    from agent_loop.session_title import title_path

    title_path(ws, sid, home=home).write_text(
        json.dumps({"title": query[:60], "title_is_manual": False}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert read_title(ws, sid, home=home)["title"].startswith("http")
    meta = ensure_auto_title(ws, sid, home=home)
    assert meta["title"].startswith("看下这段对话")
    assert "http" not in meta["title"]


def test_spark_home_override(tmp_path):
    other = tmp_path / "other-home"
    path = session_log_path_for(tmp_path, "s1", home=other)
    path.resolve().relative_to(other.resolve())
    assert path.name == "session.jsonl"
