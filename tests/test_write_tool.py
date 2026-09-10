"""write 工具：新建、覆盖、空文件、逃逸、目录拒绝。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_loop.runtime.tool_runtime import ToolRuntime
from agent_loop.tools.write_tool import REPLAY, execute


def test_replay_is_never():
    assert REPLAY == "never"


def test_write_creates_file(tmp_path):
    out = asyncio.run(
        execute({"path": "a.py", "content": "hello\n"}, workspace=str(tmp_path))
    )
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "hello\n"
    assert "created" in out
    assert "6 bytes" in out
    assert "1 lines" in out


def test_write_overwrites_file(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("old\n", encoding="utf-8")
    out = asyncio.run(
        execute({"path": "a.py", "content": "new\n"}, workspace=str(tmp_path))
    )
    assert target.read_text(encoding="utf-8") == "new\n"
    assert "overwritten" in out


def test_write_empty_file(tmp_path):
    out = asyncio.run(
        execute({"path": "empty.txt", "content": ""}, workspace=str(tmp_path))
    )
    assert (tmp_path / "empty.txt").read_text(encoding="utf-8") == ""
    assert "0 bytes" in out
    assert "0 lines" in out
    assert "created" in out


def test_write_mkdir_parents(tmp_path):
    asyncio.run(
        execute(
            {"path": "src/foo/bar.py", "content": "x = 1\n"},
            workspace=str(tmp_path),
        )
    )
    assert (tmp_path / "src" / "foo" / "bar.py").read_text(encoding="utf-8") == "x = 1\n"


def test_write_missing_content(tmp_path):
    with pytest.raises(ValueError, match="content"):
        asyncio.run(execute({"path": "a.py"}, workspace=str(tmp_path)))


def test_write_rejects_directory(tmp_path):
    (tmp_path / "src").mkdir()
    with pytest.raises(IsADirectoryError):
        asyncio.run(
            execute({"path": "src", "content": "nope"}, workspace=str(tmp_path))
        )


def test_write_outside_workspace(tmp_path):
    workspace = tmp_path / "ws"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    out = asyncio.run(
        execute(
            {"path": str(other / "doc.md"), "content": "hi\n"},
            workspace=str(workspace),
        )
    )
    assert "created" in out
    assert (other / "doc.md").read_text(encoding="utf-8") == "hi\n"


def test_write_relative_outside(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    asyncio.run(
        execute(
            {"path": "../outside.txt", "content": "x\n"},
            workspace=str(workspace),
        )
    )
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "x\n"


def test_runtime_write_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {
                    "name": "write",
                    "arguments": '{"path": "hello.txt", "content": "hi\\n"}',
                },
            }
        )
    )
    assert result.is_error is False
    assert "created" in result.content
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hi\n"


def test_runtime_write_outside_ok(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    runtime = ToolRuntime()
    runtime.workspace = str(workspace)
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {
                    "name": "write",
                    "arguments": '{"path": "../outside.txt", "content": "x"}',
                },
            }
        )
    )
    assert result.is_error is False
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "x"
