"""grep 工具：命中格式、glob、截断 footer、0 命中不是错误。"""
from __future__ import annotations

import asyncio

import pytest

from agent_loop.tool_runtime import ToolRuntime
from agent_loop.tools.grep_tool import MAX_SHOWN, REPLAY, execute


@pytest.fixture(autouse=True)
def _python_backend(monkeypatch):
    monkeypatch.setattr("agent_loop.tools.grep_tool.which_rg", lambda: None)


def test_replay_is_safe():
    assert REPLAY == "safe"


def test_grep_line_format(tmp_path):
    (tmp_path / "a.py").write_text("class AgentLoop:\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("class ReactAgentLoop(AgentLoop):\n", encoding="utf-8")
    out = asyncio.run(execute({"pattern": "AgentLoop"}, workspace=str(tmp_path)))
    assert "a.py:1:class AgentLoop:" in out
    assert "b.py:1:class ReactAgentLoop(AgentLoop):" in out
    assert "[Showing 2 matches]" in out


def test_grep_glob_filters(tmp_path):
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    out = asyncio.run(
        execute({"pattern": "needle", "glob": "*.py"}, workspace=str(tmp_path))
    )
    assert "a.py:1:needle" in out
    assert "a.txt" not in out
    assert "[Showing 1 match]" in out


def test_grep_skips_venv(tmp_path):
    (tmp_path / "src.py").write_text("secret\n", encoding="utf-8")
    hidden = tmp_path / ".venv" / "lib"
    hidden.mkdir(parents=True)
    (hidden / "pkg.py").write_text("secret\n", encoding="utf-8")
    out = asyncio.run(execute({"pattern": "secret"}, workspace=str(tmp_path)))
    assert "src.py:1:secret" in out
    assert ".venv" not in out


def test_grep_no_matches_is_not_error(tmp_path):
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    out = asyncio.run(execute({"pattern": "zzzz"}, workspace=str(tmp_path)))
    assert out == 'No matches for "zzzz" in .'


def test_grep_invalid_regex(tmp_path):
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad regex"):
        asyncio.run(execute({"pattern": "("}, workspace=str(tmp_path)))


def test_grep_truncates_with_total(tmp_path):
    lines = [f"needle {i}" for i in range(MAX_SHOWN + 10)]
    (tmp_path / "a.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = asyncio.run(execute({"pattern": "needle"}, workspace=str(tmp_path)))
    assert f"a.py:1:needle 0" in out
    assert f"a.py:{MAX_SHOWN}:needle {MAX_SHOWN - 1}" in out
    assert f"a.py:{MAX_SHOWN + 1}:needle {MAX_SHOWN}" not in out
    assert f"[Showing {MAX_SHOWN} of {MAX_SHOWN + 10} matches." in out
    assert "Narrow path, glob, or pattern.]" in out


def test_grep_path_file(tmp_path):
    (tmp_path / "a.py").write_text("hit\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("hit\n", encoding="utf-8")
    out = asyncio.run(
        execute({"pattern": "hit", "path": "a.py"}, workspace=str(tmp_path))
    )
    assert "a.py:1:hit" in out
    assert "b.py" not in out


def test_grep_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            execute({"pattern": "x", "path": "nope"}, workspace=str(tmp_path))
        )


def test_runtime_grep_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.py").write_text("alpha\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {
                    "name": "grep",
                    "arguments": '{"pattern": "alpha"}',
                },
            }
        )
    )
    assert result.is_error is False
    assert "hello.py:1:alpha" in result.content


def test_runtime_empty_pattern_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {
                    "name": "grep",
                    "arguments": '{"pattern": ""}',
                },
            }
        )
    )
    assert result.is_error is True
    assert "pattern 为空" in result.content
