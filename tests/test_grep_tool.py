"""grep 工具：命中格式、glob、截断 footer、0 命中不是错误。"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from agent_loop.session_log import SessionLog, session_log_path
from agent_loop.runtime.tool_runtime import ToolRuntime
from agent_loop.tools.grep_tool import PAGE_SIZE, REPLAY, execute

_CURSOR = re.compile(r'cursor="(g1\.[0-9a-f]{12}\.\d+)"')


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


def test_grep_paginates_from_snapshot(tmp_path):
    session_dir = str(tmp_path / ".agent" / "sessions" / "s1")
    total = PAGE_SIZE + 10
    lines = [f"needle {i}" for i in range(total)]
    (tmp_path / "a.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = asyncio.run(
        execute({"pattern": "needle"}, workspace=str(tmp_path), session_dir=session_dir)
    )
    assert "a.py:1:needle 0" in out
    assert f"a.py:{PAGE_SIZE}:needle {PAGE_SIZE - 1}" in out
    assert f"a.py:{PAGE_SIZE + 1}:needle {PAGE_SIZE}" not in out
    assert f"[Showing {PAGE_SIZE} of {total} matches." in out
    matched = _CURSOR.search(out)
    assert matched, out
    cursor = matched.group(1)
    grep_dir = Path(session_dir) / "workspace" / "tools_result" / "grep"
    assert list(grep_dir.iterdir())

    page2 = asyncio.run(
        execute({"cursor": cursor}, workspace=str(tmp_path), session_dir=session_dir)
    )
    assert f"a.py:{PAGE_SIZE + 1}:needle {PAGE_SIZE}" in page2
    assert "a.py:1:needle 0" not in page2
    assert "Continue with cursor" not in page2
    assert f"[Showing 10 of {total} matches]" in page2


def test_grep_cursor_pattern_mismatch(tmp_path):
    session_dir = str(tmp_path / ".agent" / "sessions" / "s1")
    (tmp_path / "a.py").write_text("\n".join(f"needle {i}" for i in range(25)) + "\n", encoding="utf-8")
    out = asyncio.run(
        execute({"pattern": "needle"}, workspace=str(tmp_path), session_dir=session_dir)
    )
    cursor = _CURSOR.search(out).group(1)
    with pytest.raises(ValueError, match="does not match pattern"):
        asyncio.run(
            execute(
                {"pattern": "other", "cursor": cursor},
                workspace=str(tmp_path),
                session_dir=session_dir,
            )
        )


def test_grep_bad_cursor(tmp_path):
    session_dir = str(tmp_path / ".agent" / "sessions" / "s1")
    with pytest.raises(ValueError, match="bad cursor"):
        asyncio.run(
            execute({"cursor": "nope"}, workspace=str(tmp_path), session_dir=session_dir)
        )


def test_session_log_path_layout(tmp_path):
    path = session_log_path({"sessionId": "wy1", "workspace": str(tmp_path)})
    assert path == tmp_path / ".agent" / "sessions" / "wy1" / "session.jsonl"


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


def test_runtime_cursor_only_next_page(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text(
        "\n".join(f"needle {i}" for i in range(25)) + "\n", encoding="utf-8"
    )
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    runtime.attach_log(
        SessionLog(session_log_path({"sessionId": "s1", "workspace": str(tmp_path)}))
    )
    first = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {"name": "grep", "arguments": '{"pattern": "needle"}'},
            }
        )
    )
    assert first.is_error is False
    cursor = _CURSOR.search(first.content).group(1)
    second = asyncio.run(
        runtime.call_tool(
            {
                "id": "c2",
                "function": {"name": "grep", "arguments": json.dumps({"cursor": cursor})},
            }
        )
    )
    assert second.is_error is False
    assert "needle 20" in second.content
    assert "needle 0" not in second.content
