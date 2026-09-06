"""read 工具：行号、offset/limit、路径解析。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_loop.tool_runtime import ToolRuntime
from agent_loop.tools.read_tool import execute, resolve_path


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_resolve_path_allows_outside(tmp_path):
    got = resolve_path("../secret", str(tmp_path))
    assert got == (tmp_path / ".." / "secret").resolve()


def test_read_outside_workspace(tmp_path):
    workspace = tmp_path / "ws"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    (other / "ref.py").write_text("x = 1\n", encoding="utf-8")
    out = asyncio.run(
        execute({"path": str(other / "ref.py")}, workspace=str(workspace))
    )
    assert "     1|x = 1" in out


def test_read_numbers_lines(tmp_path):
    _write(tmp_path, "a.py", "import os\ndef main():\n    pass\n")
    out = asyncio.run(execute({"path": "a.py"}, workspace=str(tmp_path)))
    assert "     1|import os" in out
    assert "     2|def main():" in out
    assert "Showing lines 1-3 of 3" in out


def test_read_offset_limit(tmp_path):
    _write(tmp_path, "a.txt", "a\nb\nc\nd\n")
    out = asyncio.run(
        execute({"path": "a.txt", "offset": 2, "limit": 2}, workspace=str(tmp_path))
    )
    assert "     2|b" in out
    assert "     3|c" in out
    assert "     1|a" not in out
    assert "Use offset=4 to continue" in out


def test_read_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        asyncio.run(execute({"path": "nope.txt"}, workspace=str(tmp_path)))


def test_read_offset_beyond_end(tmp_path):
    _write(tmp_path, "a.txt", "only\n")
    with pytest.raises(ValueError, match="beyond end"):
        asyncio.run(execute({"path": "a.txt", "offset": 9}, workspace=str(tmp_path)))


def test_runtime_read_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {
                    "name": "read",
                    "arguments": '{"path": "hello.txt"}',
                },
            }
        )
    )
    assert result.is_error is False
    assert "     1|hi" in result.content
