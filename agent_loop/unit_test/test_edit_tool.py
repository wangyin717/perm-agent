"""edit 工具：唯一替换、replace_all、失败文案。"""
from __future__ import annotations

import asyncio

import pytest

from agent_loop.tool_runtime import ToolRuntime
from agent_loop.tools.edit_tool import REPLAY, execute


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
    assert out == "Replaced 1 occurrence in a.py"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "foo = 2\nbar = 2\n"


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
    assert out == "Replaced 2 occurrences in a.py"
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


def test_runtime_edit_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.py").write_text("hi\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {
                    "name": "edit",
                    "arguments": '{"path": "hello.py", "old": "hi", "new": "hey"}',
                },
            }
        )
    )
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
