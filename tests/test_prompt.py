"""system 组装：人设 + cwd + 日期 + 可选 AGENTS.md。"""
from __future__ import annotations

from datetime import date

from agent_loop.prompt.assemble import _AGENTS_MAX_CHARS, build_system_prompt


def test_includes_persona_cwd_and_date(tmp_path):
    text = build_system_prompt(str(tmp_path), today=date(2026, 9, 10))
    assert "spark-agent" in text
    assert "# Tool notes" in text
    assert "Prefer specialized tools over" in text
    assert "Do not use bash to read" in text
    assert "mdfind -name" in text
    assert "Never `find $HOME`" in text
    assert "memory_search" in text
    assert "offset" not in text  # 工具参数留给 schema
    assert f"Current working directory: {tmp_path.resolve().as_posix()}" in text
    assert "Today's date: 2026-09-10 (Thursday)" in text


def test_appends_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Use pytest.\n", encoding="utf-8")
    text = build_system_prompt(str(tmp_path), today=date(2026, 9, 10))
    assert "# Project instructions (AGENTS.md)" in text
    assert "Use pytest." in text
    # 人设在前，cwd/日期在后，方便前缀缓存
    assert text.index("spark-agent") < text.index("Use pytest.")
    assert text.index("Use pytest.") < text.index("Current working directory:")


def test_tool_notes_only_for_enabled_tools(tmp_path):
    bash_only = build_system_prompt(
        str(tmp_path), today=date(2026, 9, 10), enabled_tools=["bash"]
    )
    assert "Never `find $HOME`" in bash_only
    assert "memory_search" not in bash_only

    mem_only = build_system_prompt(
        str(tmp_path), today=date(2026, 9, 10), enabled_tools=["memory_search"]
    )
    assert "memory_search" in mem_only
    assert "Never `find $HOME`" not in mem_only

    none = build_system_prompt(
        str(tmp_path), today=date(2026, 9, 10), enabled_tools=[]
    )
    assert "# Tool notes" not in none
    assert "memory_search" not in none


def test_truncates_long_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x" * (_AGENTS_MAX_CHARS + 50), encoding="utf-8")
    text = build_system_prompt(str(tmp_path), today=date(2026, 9, 10))
    assert "AGENTS.md truncated" in text
    assert "x" * 40 in text
