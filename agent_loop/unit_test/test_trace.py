from agent_loop.trace import one_line, summarize_args, summarize_result


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
