from agent_loop.trace import (
    format_tool_line,
    format_tool_line_from_call,
    log_llm_text,
    log_llm_tools,
    log_user,
    one_line,
    summarize_args,
    summarize_result,
)


def test_one_line_collapses_newlines():
    assert one_line("total 24\ndrwxr-xr-x  11 wangyin") == "total 24 drwxr-xr-x 11 wangyin"


def test_one_line_clips():
    out = one_line("a" * 200, limit=10)
    assert out.startswith("aaaaaaaaaa")
    assert out.endswith("(200 chars)")


def test_summarize_args_prefers_cmd():
    assert summarize_args({"cmd": "ls -la"}) == "ls -la"


def test_summarize_args_path_with_offset():
    assert summarize_args({"path": "a.py", "offset": 10, "limit": 20}) == (
        "a.py (offset=10, limit=20)"
    )


def test_summarize_args_write_skips_content():
    assert summarize_args({"path": "a.py", "content": "hello\nworld"}) == "a.py"


def test_summarize_result_first_line():
    content = "     1|# agent-loop\n     2|\n     3|从 SwiftAgent 拆出"
    assert summarize_result(content) == "1|# agent-loop"


def test_format_read_with_range():
    assert format_tool_line("read", {"path": "a.py", "offset": 1, "limit": 69}) == (
        "- Read: a.py (1-69)"
    )


def test_format_read_offset_only():
    assert format_tool_line("read", {"path": "a.py", "offset": 10}) == "- Read: a.py (10-)"


def test_format_read_path_only():
    assert format_tool_line("read", {"path": "a.py"}) == "- Read: a.py"


def test_format_write_skips_content():
    assert format_tool_line("write", {"path": "a.py", "content": "hello\nworld"}) == (
        "- Write: a.py"
    )


def test_format_bash_cmd():
    assert format_tool_line("bash", {"cmd": "ls -la"}) == "- Bash: ls -la"


def test_format_edit_skips_old_new():
    assert format_tool_line(
        "edit",
        {"path": "a.py", "old": "foo\nbar", "new": "baz"},
    ) == "- Edit: a.py"


def test_format_grep_pattern():
    assert format_tool_line("grep", {"pattern": "class AgentLoop"}) == (
        "- Grep: class AgentLoop"
    )


def test_format_grep_with_path_and_glob():
    assert format_tool_line(
        "grep",
        {"pattern": "needle", "path": "src", "glob": "*.py"},
    ) == "- Grep: needle (src, *.py)"


def test_format_tool_from_openai_call():
    call = {
        "function": {
            "name": "read",
            "arguments": '{"path": "CONTEXT.md", "offset": 1, "limit": 69}',
        }
    }
    assert format_tool_line_from_call(call) == "- Read: CONTEXT.md (1-69)"


def test_log_sections(capsys):
    log_user("hello\nworld")
    log_llm_text("先读文件。")
    log_llm_tools(
        [
            {
                "function": {
                    "name": "read",
                    "arguments": '{"path": "a.py", "offset": 1, "limit": 69}',
                }
            },
            {"function": {"name": "bash", "arguments": '{"cmd": "ls -la"}'}},
        ]
    )
    out = capsys.readouterr().out
    assert out == (
        "## User\n"
        "\n"
        "hello\n"
        "world\n"
        "\n"
        "## Assistant\n"
        "\n"
        "先读文件。\n"
        "\n"
        "## Tools\n"
        "\n"
        "- Read: a.py (1-69)\n"
        "- Bash: ls -la\n"
        "\n"
    )


def test_log_llm_text_skips_empty(capsys):
    log_llm_text("")
    log_llm_text("   ")
    assert capsys.readouterr().out == ""
