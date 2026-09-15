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
    assert "#6e6e73" in out
    long = "x" * 200
    assert _clip_arg(long, 10).endswith("…")
    assert len(_clip_arg(long, 10)) <= 10


def test_highlight_code_line_python_keywords():
    from agent_loop.cli.tui import highlight_code_line

    text = highlight_code_line("def foo():", "a.py")
    assert text.plain.startswith("def")
    assert "foo" in text.plain
    assert len(text.spans) >= 2


def test_format_grok_tool():
    assert "Read" in format_grok_tool("read", {"path": "a/tui.py"})
    assert "tui.py" in format_grok_tool("read", {"path": "a/tui.py"})
    assert "Search" in format_grok_tool("grep", {"pattern": "TODO"})
    assert "Edit" in format_grok_tool("edit", {"path": "loop.py"})


def test_parse_slash():
    assert parse_slash("/exit") == "exit"
    assert parse_slash("/quit") == "exit"
    assert parse_slash("/new") == "new"
    assert parse_slash("/help") == "help"
    assert parse_slash("/session") == "session"
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
