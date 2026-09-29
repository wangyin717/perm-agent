"""事件口：订阅能收到；关掉 stdout 时 trace 不 print。"""
from __future__ import annotations

from agent_loop import events
from agent_loop.cli.tui import format_grok_tool, parse_slash
from agent_loop.cli.trace import log_context, log_tool_result, log_tool_start, log_user


def test_bold_verb_only_first_word():
    from agent_loop.cli.tui import _bold_verb, _clip_arg

    out = _bold_verb("Read  tui.py")
    assert out.startswith("[bold]Read[/]")
    assert "tui.py" in out
    assert "#767676" in out
    long = "x" * 200
    assert _clip_arg(long, 10).endswith("…")
    assert len(_clip_arg(long, 10)) <= 10


def _span_covering(text, column: int):
    return next(span for span in text.spans if span.start <= column < span.end)


def test_highlight_code_line_python_keywords():
    from agent_loop.cli.theme import set_palette
    from agent_loop.cli.tui import highlight_code_line

    set_palette("day")
    text = highlight_code_line("def foo():", "a.py")
    assert text.plain == "def foo():"
    assert "7d4bc6" in str(_span_covering(text, 0).style).lower()
    foo = text.plain.index("foo")
    assert "2f64d2" in str(_span_covering(text, foo).style).lower()
    assert "5580a8" in str(_span_covering(text, text.plain.index("(")).style).lower()

    quoted = highlight_code_line('x = "hi"', "a.py")
    assert "378e23" in str(_span_covering(quoted, quoted.plain.index("hi")).style).lower()
    number = highlight_code_line("n = 14", "a.py")
    assert "c3691e" in str(_span_covering(number, number.plain.index("1")).style).lower()
    comment = highlight_code_line("# note", "a.py")
    comment_style = str(comment.spans[0].style).lower()
    assert "italic" in comment_style
    assert "909090" in comment_style
    indented = highlight_code_line("\treturn", "a.py")
    assert indented.plain.startswith("    return")


def test_html_highlight_matches_grok_colors():
    from agent_loop.cli.theme import set_palette
    from agent_loop.cli.tui import highlight_code_line, highlight_diff_rows

    try:
        set_palette("day")
        line = highlight_code_line('<div class="board">', "breakout.html")
        assert line.plain == '<div class="board">'
        assert "cd3048" in str(_span_covering(line, line.plain.index("div")).style).lower()
        assert "7d4bc6" in str(_span_covering(line, line.plain.index("class")).style).lower()
        assert "378e23" in str(_span_covering(line, line.plain.index("board")).style).lower()
        assert "5580a8" in str(_span_covering(line, 0).style).lower()
        entity = highlight_code_line("a &amp; b", "breakout.html")
        assert "0f87a2" in str(_span_covering(entity, entity.plain.index("&")).style).lower()

        src = (
            "<script>\n"
            "const keep = 1;\n"
            "const keep2 = 2;\n"
            "const keep3 = 3;\n"
            "const a = 1;\n"
            "const tail = 4;\n"
            "function shade(hex) { return hex; }\n"
            "</script>\n"
        )
        rows = [
            {"kind": "ctx", "ln": 4, "body": "const keep3 = 3;"},
            {"kind": "del", "ln": 5, "body": "const a = 1;"},
            {"kind": "add", "ln": 5, "body": "const a = 2;"},
            {"kind": "ctx", "ln": 7, "body": "function shade(hex) { return hex; }"},
        ]
        updated = src.replace("const a = 1;", "const a = 2;")
        painted = highlight_diff_rows(rows, "breakout.html", src, updated)
        alone = highlight_code_line("const keep3 = 3;", "breakout.html")
        assert "7d4bc6" not in str(alone.spans[0].style).lower()
        assert "7d4bc6" in str(_span_covering(painted[0], 0).style).lower()
        assert "7d4bc6" in str(_span_covering(painted[1], 0).style).lower()
        assert "7d4bc6" in str(_span_covering(painted[2], 0).style).lower()
        shade_at = painted[3].plain.index("shade")
        assert "2f64d2" in str(_span_covering(painted[3], shade_at).style).lower()
        hex_at = painted[3].plain.index("hex")
        assert "444444" in str(_span_covering(painted[3], hex_at).style).lower()

        tabbed = "function\tshade() {\n\treturn 1;\n}\n"
        tab_rows = [{"kind": "add", "ln": 2, "body": "\treturn 1;"}]
        tab_paint = highlight_diff_rows(tab_rows, "a.js", None, tabbed)
        assert tab_paint[0].plain.startswith("    return")
        assert "7d4bc6" in str(_span_covering(tab_paint[0], 4).style).lower()

        drifted = highlight_diff_rows(
            [{"kind": "del", "ln": 1, "body": "const a = 1;"}],
            "breakout.html",
            "nope\n",
            None,
        )
        assert "7d4bc6" not in str(drifted[0].spans[0].style).lower()

        set_palette("night")
        night = highlight_code_line('<div class="board">', "breakout.html")
        assert "f7768e" in str(_span_covering(night, night.plain.index("div")).style).lower()
        assert "bb9af7" in str(_span_covering(night, night.plain.index("class")).style).lower()
    finally:
        set_palette("day")


def test_html_edit_event_carries_the_whole_file(tmp_path):
    import asyncio

    from agent_loop.tools.edit_tool import execute
    from agent_loop.cli.tui import highlight_diff_rows

    src = (
        "<script>\n"
        "const keep = 1;\n"
        "const keep2 = 2;\n"
        "const keep3 = 3;\n"
        "const a = 1;\n"
        "const tail = 4;\n"
        "</script>\n"
    )
    (tmp_path / "breakout.html").write_text(src, encoding="utf-8")
    seen = []
    unsub = events.subscribe(lambda event: seen.append(event))
    try:
        asyncio.run(
            execute(
                {"path": "breakout.html", "old": "const a = 1;", "new": "const a = 2;"},
                workspace=str(tmp_path),
            )
        )
    finally:
        unsub()
    diff = next(event for event in seen if event.get("kind") == "edit_diff")
    assert diff["before"] == src
    assert "const a = 2;" in diff["after"]
    ctx = next(row for row in diff["rows"] if row["body"] == "const keep3 = 3;")
    painted = highlight_diff_rows(
        [ctx], diff["path"], diff["before"], diff["after"]
    )
    assert "7d4bc6" in str(_span_covering(painted[0], 0).style).lower()


def test_highlight_document_skips_huge_files(monkeypatch):
    from agent_loop.cli import tui

    monkeypatch.setattr(tui, "_HL_MAX_BYTES", 8)
    assert tui.highlight_document("const a = 1;\n", "a.js", {1}) is None


def test_diff_line_numbers_use_grok_red_and_green():
    from agent_loop.cli.theme import set_palette
    from agent_loop.cli.tui import diff_line_text

    def gutter_style(kind: str) -> str:
        text = diff_line_text(kind, 4, "def foo():", "a.py", gutter_width=3)
        return str(text.spans[0].style).lower()

    def code_band(kind: str) -> str:
        text = diff_line_text(kind, 4, "def foo():", "a.py", gutter_width=3)
        gutter_end = len(f"{4:>{3}}  ")
        styles = " ".join(
            str(span.style).lower()
            for span in text.spans
            if span.start <= gutter_end < span.end
        )
        return styles

    try:
        set_palette("day")
        assert diff_line_text("add", 4, "x", "a.py", gutter_width=3).plain.startswith("  4  ")
        assert "cd3048" in gutter_style("del")
        assert "378e23" in gutter_style("add")
        assert "767676" in gutter_style("ctx")
        assert "7d4bc6" in code_band("add")
        assert "daf2dc" not in code_band("add")
        assert "f5dade" not in code_band("del")
        from agent_loop.cli.tui import DiffLine, SparkTui

        assert "background: $diff-add" in DiffLine.DEFAULT_CSS
        assert "background: $diff-del" in DiffLine.DEFAULT_CSS
        assert "#timeline DiffLine.add { background: $diff-add; }" in SparkTui.CSS
        assert "#timeline DiffLine.del { background: $diff-del; }" in SparkTui.CSS
        spaces = diff_line_text("add", 90, "    tr = 1", "a.py", gutter_width=3)
        assert "    tr" in spaces.plain
        set_palette("night")
        assert "f7768e" in gutter_style("del")
        assert "9ece6a" in gutter_style("add")
        assert "6c6c6c" in gutter_style("ctx")
    finally:
        set_palette("day")


def test_format_grok_tool():
    assert "Read" in format_grok_tool("read", {"path": "a/tui.py"})
    assert "tui.py" in format_grok_tool("read", {"path": "a/tui.py"})
    assert "Search" in format_grok_tool("grep", {"pattern": "TODO"})
    assert "Edit" in format_grok_tool("edit", {"path": "loop.py"})
    assert (
        format_grok_tool("browser_exec", {"code": "print(page_info())\nprint(2)"})
        == "browser_exec  print(page_info())"
    )
    assert format_grok_tool("browser_screenshot", {}) == "browser_screenshot"
    assert format_grok_tool("computer", {"name": "click"}) == "computer  click"


def test_parse_slash():
    assert parse_slash("/exit") == "exit"
    assert parse_slash("/quit") == "exit"
    assert parse_slash("/new") == "new"
    assert parse_slash("/help") == "help"
    assert parse_slash("/model") == "model"
    assert parse_slash("/session") == "session"
    assert parse_slash("/resume") == "resume"
    assert parse_slash("/resume 2") == "resume"
    assert parse_slash("/rename hello") == "rename"
    assert parse_slash("/title x") == "rename"
    assert parse_slash("hello") is None
    assert parse_slash("/unknown") is None


def test_subscribe_receives_emit():
    seen = []
    unsub = events.subscribe(lambda e: seen.append(e))
    try:
        events.emit("user", text="hi")
    finally:
        unsub()
    assert seen == [{"kind": "user", "text": "hi"}]


def test_trace_emits_and_can_silence_stdout(capsys, monkeypatch):
    monkeypatch.setattr(events, "print_to_stdout", False)
    seen = []
    unsub = events.subscribe(lambda e: seen.append(e["kind"]))
    try:
        log_user("hello")
        log_context(1000, 2000)
    finally:
        unsub()
        monkeypatch.setattr(events, "print_to_stdout", True)
    assert "user" in seen
    assert "context" in seen
    assert capsys.readouterr().out == ""


def test_tool_start_and_result_emit(monkeypatch):
    monkeypatch.setattr(events, "print_to_stdout", False)
    seen = []
    unsub = events.subscribe(lambda e: seen.append(e["kind"]))
    try:
        log_tool_start("grep", {"pattern": "TODO"})
        log_tool_result("grep", "a.py:1:TODO", False)
    finally:
        unsub()
        monkeypatch.setattr(events, "print_to_stdout", True)
    assert seen == ["tool_start", "tool_result"]
