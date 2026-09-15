"""edit 工具：唯一替换、replace_all、失败文案。"""
from __future__ import annotations

import asyncio

import pytest

from agent_loop import events
from agent_loop.runtime.tool_runtime import ToolRuntime
from agent_loop.tools.edit_tool import REPLAY, execute, format_edit_hunk


def test_format_edit_hunk_has_context_and_signs():
    text = "a\nb\nc\nold\nd\ne\n"
    plain, marked = format_edit_hunk(text, "old", "new", text.find("old"))
    assert " b|" in plain or "|b" in plain
    assert "-   4|old" in plain
    assert "+   4|new" in plain
    assert "on #f5dade" in marked
    assert "on #daf2dc" in marked


def test_replay_is_never():
    assert REPLAY == "never"


def test_edit_unique_replace(tmp_path):
    (tmp_path / "a.py").write_text("foo = 1\nbar = 2\n", encoding="utf-8")
    out = asyncio.run(
        execute(
            {"path": "a.py", "old": "foo = 1", "new": "foo = 2"},
            workspace=str(tmp_path),
        )
    )
    assert "Replaced 1 occurrence in a.py" in out
    assert "-   1|foo = 1" in out
    assert "+   1|foo = 2" in out
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "foo = 2\nbar = 2\n"


def test_edit_emits_ui_diff(tmp_path):
    seen = []
    unsub = events.subscribe(lambda e: seen.append(e))
    try:
        (tmp_path / "a.py").write_text("foo = 1\n", encoding="utf-8")
        asyncio.run(
            execute(
                {"path": "a.py", "old": "foo = 1", "new": "foo = 2"},
                workspace=str(tmp_path),
            )
        )
    finally:
        unsub()
    diffs = [e for e in seen if e.get("kind") == "edit_diff"]
    assert len(diffs) == 1
    assert "foo = 2" in diffs[0]["hunk"]


def test_edit_replace_all(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    out = asyncio.run(
        execute(
            {
                "path": "a.py",
                "old": "x = 1",
                "new": "x = 2",
                "replace_all": True,
            },
            workspace=str(tmp_path),
        )
    )
    assert "Replaced 2 occurrences in a.py" in out
    assert "… and 1 more" in out
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\nx = 2\n"


def test_edit_empty_new_deletes(tmp_path):
    (tmp_path / "a.py").write_text("keep\ndrop\nkeep\n", encoding="utf-8")
    asyncio.run(
        execute(
            {"path": "a.py", "old": "drop\n", "new": ""},
            workspace=str(tmp_path),
        )
    )
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "keep\nkeep\n"


def test_edit_not_found(tmp_path):
    (tmp_path / "a.py").write_text("foo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="old not found"):
        asyncio.run(
            execute(
                {"path": "a.py", "old": "bar", "new": "baz"},
                workspace=str(tmp_path),
            )
        )


def test_edit_not_unique_includes_line_numbers(tmp_path):
    (tmp_path / "a.py").write_text("aa\nxx\naa\nyy\naa\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"old found 3 times in a.py \(lines 1, 3, 5\)"):
        asyncio.run(
            execute(
                {"path": "a.py", "old": "aa", "new": "bb"},
                workspace=str(tmp_path),
            )
        )
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "aa\nxx\naa\nyy\naa\n"


def test_edit_old_equals_new(tmp_path):
    (tmp_path / "a.py").write_text("foo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="old and new are the same"):
        asyncio.run(
            execute(
                {"path": "a.py", "old": "foo", "new": "foo"},
                workspace=str(tmp_path),
            )
        )


def test_edit_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            execute(
                {"path": "nope.py", "old": "a", "new": "b"},
                workspace=str(tmp_path),
            )
        )


def test_edit_rejects_directory(tmp_path):
    (tmp_path / "src").mkdir()
    with pytest.raises(IsADirectoryError):
        asyncio.run(
            execute(
                {"path": "src", "old": "a", "new": "b"},
                workspace=str(tmp_path),
            )
        )


def test_edit_outside_workspace(tmp_path):
    workspace = tmp_path / "ws"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    (other / "a.py").write_text("old\n", encoding="utf-8")
    out = asyncio.run(
        execute(
            {"path": str(other / "a.py"), "old": "old", "new": "new"},
            workspace=str(workspace),
        )
    )
    assert "Replaced 1 occurrence" in out
    assert (other / "a.py").read_text(encoding="utf-8") == "new\n"


def _call(runtime, name, arguments, call_id="c1"):
    return asyncio.run(
        runtime.call_tool(
            {
                "id": call_id,
                "function": {"name": name, "arguments": arguments},
            }
        )
    )


def test_runtime_edit_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.py").write_text("hi\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    assert _call(runtime, "read", '{"path": "hello.py"}').is_error is False
    result = _call(runtime, "edit", '{"path": "hello.py", "old": "hi", "new": "hey"}', "c2")
    assert result.is_error is False
    assert "Replaced 1 occurrence" in result.content
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "hey\n"


def test_runtime_empty_old_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {
                    "name": "edit",
                    "arguments": '{"path": "a.py", "old": "", "new": "y"}',
                },
            }
        )
    )
    assert result.is_error is True
    assert "old 为空" in result.content
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x\n"


def test_edit_without_read_is_rejected(tmp_path):
    (tmp_path / "a.py").write_text("foo\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    result = _call(runtime, "edit", '{"path": "a.py", "old": "foo", "new": "bar"}')
    assert result.is_error is True
    assert "Read a.py before editing" in result.content
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "foo\n"


def test_paginated_read_allows_edit(tmp_path):
    lines = [f"L{i:04d}" for i in range(1500)]
    (tmp_path / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    first = _call(runtime, "read", '{"path": "big.py"}')
    assert "Use offset=" in first.content
    result = _call(
        runtime, "edit", '{"path": "big.py", "old": "L1499", "new": "DONE"}', "c2"
    )
    assert result.is_error is False
    assert "DONE" in (tmp_path / "big.py").read_text(encoding="utf-8")


def test_second_edit_does_not_need_another_read(tmp_path):
    (tmp_path / "a.py").write_text("foo\nbar\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    _call(runtime, "read", '{"path": "a.py"}')
    assert _call(runtime, "edit", '{"path": "a.py", "old": "foo", "new": "FOO"}', "c2").is_error is False
    result = _call(runtime, "edit", '{"path": "a.py", "old": "bar", "new": "BAR"}', "c3")
    assert result.is_error is False
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "FOO\nBAR\n"


def test_external_change_requires_reread(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("foo\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    _call(runtime, "read", '{"path": "a.py"}')
    target.write_text("foo\nchanged\n", encoding="utf-8")
    result = _call(runtime, "edit", '{"path": "a.py", "old": "foo", "new": "bar"}', "c2")
    assert result.is_error is True
    assert "changed on disk" in result.content
    assert target.read_text(encoding="utf-8") == "foo\nchanged\n"
    _call(runtime, "read", '{"path": "a.py"}', "c3")
    result = _call(runtime, "edit", '{"path": "a.py", "old": "foo", "new": "bar"}', "c4")
    assert result.is_error is False


def test_mtime_bump_same_content_allows_edit(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("foo\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    _call(runtime, "read", '{"path": "a.py"}')
    target.write_text("foo\n", encoding="utf-8")
    result = _call(runtime, "edit", '{"path": "a.py", "old": "foo", "new": "bar"}', "c2")
    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "bar\n"


def test_write_then_edit_without_read(tmp_path):
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    created = _call(runtime, "write", '{"path": "n.py", "content": "foo\\n"}')
    assert created.is_error is False
    result = _call(runtime, "edit", '{"path": "n.py", "old": "foo", "new": "bar"}', "c2")
    assert result.is_error is False


def test_overwrite_without_read_is_rejected(tmp_path):
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    result = _call(runtime, "write", '{"path": "a.py", "content": "new\\n"}')
    assert result.is_error is True
    assert "Read a.py before editing" in result.content
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "old\n"


def test_pdf_read_does_not_unlock_edit(tmp_path, monkeypatch):
    from agent_loop.tools import read_tool

    monkeypatch.setitem(read_tool.EXTRACTORS, ".pdf", lambda _p: "Invoice")
    (tmp_path / "inv.pdf").write_bytes(b"%PDF-\x00fake")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    assert _call(runtime, "read", '{"path": "inv.pdf"}').is_error is False
    (tmp_path / "inv.pdf").write_text("not really a pdf\n", encoding="utf-8")
    result = _call(
        runtime, "edit", '{"path": "inv.pdf", "old": "not really", "new": "x"}', "c2"
    )
    assert result.is_error is True
    assert "Read inv.pdf before editing" in result.content
