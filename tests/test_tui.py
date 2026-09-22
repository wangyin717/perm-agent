"""TUI：流思考在时间线先 Thinking 再落成 Thought。"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from agent_loop.cli.tui import (
    PromptInput,
    SparkTui,
    ThoughtBody,
    TimelineRow,
    WaitingMark,
    WaitingNotice,
    _clip_thought,
    matching_slash,
    slash_prefix,
)


def _status(app: SparkTui) -> str:
    nodes = app.query("#status")
    return str(nodes[0].content) if nodes else ""


def _thoughts(app: SparkTui) -> list[str]:
    return [str(w.content) for w in app.query(TimelineRow) if "thought" in w.classes]


async def _run() -> None:
    app = SparkTui("think-test", "/tmp/spark-agent-tui-think")
    async with app.run_test(size=(100, 30)) as pilot:
        app._busy = True
        app._busy_t0 = time.monotonic()
        app._start_turn("hello")
        await pilot.pause()
        assert "Waiting" not in _status(app), _status(app)
        assert _thoughts(app) == []

        app._handle_event(
            {"kind": "assistant_delta", "text": "let me think", "channel": "reasoning"}
        )
        app._handle_event(
            {
                "kind": "assistant_delta",
                "text": " and keep the rest of this sentence",
                "channel": "reasoning",
            }
        )
        await pilot.pause()
        live = _thoughts(app)
        assert len(live) == 1, live
        assert "Thinking" in live[0]
        assert "Thought" not in live[0]
        assert "Waiting" not in _status(app), _status(app)
        assert list(app.query(ThoughtBody)) == []

        app._handle_event({"kind": "assistant_delta", "text": "hello", "channel": "content"})
        await pilot.pause()
        done = _thoughts(app)
        assert len(done) == 1
        assert "Thought" in done[0]
        assert "for" in done[0]
        assert list(app.query(ThoughtBody)) == []
        assert "let me think" not in done[0]

        app._handle_event({"kind": "tools", "tool_calls": [{"name": "read"}]})
        app._handle_event({"kind": "tool_start", "name": "read", "args": {"path": "a.py"}})
        await asyncio.sleep(0.15)
        await pilot.pause()
        assert len(_thoughts(app)) == 1

        app._handle_event({"kind": "tool_result", "name": "read", "is_error": False})
        await pilot.pause()
        assert "Waiting" not in _status(app), _status(app)
        assert len(_thoughts(app)) == 1

        app._handle_event({"kind": "assistant_delta", "text": "done", "channel": "content"})
        await pilot.pause()
        assert len(_thoughts(app)) == 1
        assert "Thought" in _thoughts(app)[0]


def test_markdown_body_uses_grokday_md_text():
    from agent_loop.cli import tui as tui_mod

    tui_mod._MD_CACHE.clear()
    text = tui_mod._markdown_as_text("hello world", 40)
    styles = " ".join(str(span.style) for span in text.spans)
    assert "#444444" in styles
    assert "hello world" in text.plain


def test_markdown_as_text_cache_returns_copy():
    from agent_loop.cli import tui as tui_mod

    tui_mod._MD_CACHE.clear()
    src = "**hello** `x.py`"
    a = tui_mod._markdown_as_text(src, 40)
    b = tui_mod._markdown_as_text(src, 40)
    assert a.plain == b.plain
    assert a is not b
    assert len(tui_mod._MD_CACHE) == 1


def test_file_logging_writes_under_permanent_home(tmp_path, monkeypatch):
    from agent_loop.file_logging import configure_file_logging

    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    path = configure_file_logging(tmp_path)
    assert path == tmp_path / "permanent.log"
    logging.getLogger("agent_loop.test").info("hello-file-log")
    for handler in logging.getLogger().handlers:
        if getattr(handler, "_permanent_file_log", False):
            handler.flush()
    assert "hello-file-log" in path.read_text(encoding="utf-8")


def test_quiet_stdio_logging_hides_warnings(capsys):
    from agent_loop.cli.tui import _quiet_stdio_logging, _restore_stdio_logging

    logger = logging.getLogger()
    logger.setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        logging.warning("visible-before")
        assert "visible-before" in capsys.readouterr().err
        saved = _quiet_stdio_logging()
        try:
            logging.warning("hidden-traceback-line")
            assert "hidden-traceback-line" not in capsys.readouterr().err
        finally:
            _restore_stdio_logging(saved)
        logging.warning("visible-after")
        assert "visible-after" in capsys.readouterr().err
    finally:
        logger.removeHandler(handler)


def test_quiet_stdio_logging_drops_detached_stderr(capsys):
    from agent_loop.cli.tui import _quiet_stdio_logging, _restore_stdio_logging

    logger = logging.getLogger()
    stale = logging.StreamHandler(stream=open("/dev/null", "w"))
    stale.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(stale)
    try:
        saved = _quiet_stdio_logging()
        assert stale in saved
        assert stale not in logger.handlers
    finally:
        _restore_stdio_logging([])
        logger.removeHandler(stale)
        stale.close()


def test_brief_exc_keeps_one_line():
    from agent_loop.runtime.tool_runtime import _brief_exc

    blob = "Traceback (most recent call last):\n  File x\nJSONDecodeError: nope"
    assert _brief_exc(RuntimeError(blob)) == "Traceback (most recent call last):"


def test_slash_row_command_is_blue():
    from agent_loop.cli.tui import _BLUE, slash_row_text

    matched = slash_row_text("/resume", "list sessions", selected=True, prefix="/re")
    assert matched.plain.startswith("› /resume")
    styles = [(matched.plain[span.start : span.end], str(span.style)) for span in matched.spans]
    assert any(part == "/re" and _BLUE in style for part, style in styles)
    assert any(part == "sume" and _BLUE not in style for part, style in styles)
    bare = slash_row_text("/help", "this list", prefix="/")
    assert _BLUE not in " ".join(str(span.style) for span in bare.spans)


def test_matching_slash_prefix():
    assert slash_prefix("hello") is None
    assert slash_prefix("/resume 1") is None
    assert slash_prefix("/") == "/"
    names = [item[0] for item in matching_slash("/")]
    assert "/help" in names and "/new" in names and "/copy" in names
    assert [item[0] for item in matching_slash("/re")] == ["/resume", "/rename"]
    assert matching_slash("/zzz") == []


def test_prompt_wraps_chevron():
    async def _run() -> None:
        app = SparkTui("prompt-test", "/tmp/spark-agent-tui-prompt")
        async with app.run_test(size=(80, 24)) as pilot:
            wrap = app.query_one("#prompt-wrap")
            mark = app.query_one("#prompt-mark")
            prompt = app.query_one("#prompt")
            assert prompt.parent is wrap
            assert mark.parent is wrap
            assert "›" in str(mark.content)
            await pilot.pause()

    asyncio.run(_run())


def test_prompt_cursor_does_not_blink():
    async def _run() -> None:
        app = SparkTui("cursor-blink", "/tmp/spark-agent-tui-cursor")
        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt", PromptInput)
            await pilot.pause()
            assert prompt.cursor_blink is False
            assert prompt._cursor_visible is True
            prompt.value = "你好"
            await pilot.pause()
            assert prompt.cursor_blink is False
            assert prompt._cursor_visible is True
            prompt._restart_blink()
            assert prompt.cursor_blink is False
            assert prompt._cursor_visible is True

    asyncio.run(_run())


def test_splash_on_empty_hides_when_turn_starts(tmp_path, monkeypatch):
    from agent_loop.cli.tui import ENDURANCE_ART, SplashCard

    home = tmp_path / "home"
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()

    async def _run() -> None:
        app = SparkTui("empty-splash", str(ws))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            cards = list(app.query(SplashCard))
            assert len(cards) == 1
            ship = str(app.query_one("#ship").content)
            ink = {ch for ch in ship if ch not in " \n"}
            assert ink == {"."}
            assert ship.splitlines()[0].strip() == ENDURANCE_ART.splitlines()[0].strip()
            assert len(ship.splitlines()) <= 16
            blurb = app.query_one("#blurb").content
            blurb_text = blurb.plain if hasattr(blurb, "plain") else str(blurb)
            assert "Permanent" in blurb_text
            assert "/new" in blurb_text
            assert "/resume" in blurb_text
            chrome = str(app.query_one("#chrome").content)
            assert chrome.startswith("Permanent")
            app._start_turn("hello")
            await pilot.pause()
            assert list(app.query(SplashCard)) == []

    asyncio.run(_run())


def test_slash_menu_filters_and_tab_completes(tmp_path, monkeypatch):
    from agent_loop.cli.tui import SlashMenu, SlashRow

    home = tmp_path / "home"
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()

    async def _run() -> None:
        app = SparkTui("slash-menu", str(ws))
        async with app.run_test(size=(100, 30)) as pilot:
            prompt = app.query_one("#prompt")
            prompt.focus()
            await pilot.press("/")
            await pilot.pause()
            menu = app.query_one("#slash-menu", SlashMenu)
            assert menu.is_open
            names = [row.cmd_name for row in app.query(SlashRow)]
            assert "/help" in names
            assert "/new" in names
            await pilot.press("r")
            await pilot.pause()
            names = [row.cmd_name for row in app.query(SlashRow)]
            assert names == ["/resume", "/rename"]
            selected = [row for row in app.query(SlashRow) if "selected" in row.classes]
            assert selected and selected[0].cmd_name == "/resume"
            assert "-slash-cmd" in prompt.classes
            await pilot.press("tab")
            await pilot.pause()
            assert prompt.value == "/resume"
            await pilot.press("escape")
            await pilot.pause()
            assert not menu.is_open
            assert prompt.value == "/resume"

            prompt.value = "/ren"
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            assert prompt.value == "/rename "
            assert not app.query_one("#slash-menu", SlashMenu).is_open

    asyncio.run(_run())


def test_resume_lists_and_replays_history(tmp_path, monkeypatch):
    from agent_loop.cli.tui import Prose, SessionPickRow, UserBanner
    from agent_loop.paths import session_log_path_for

    home = tmp_path / "home"
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    log = session_log_path_for(ws, "cli-old", home=home)
    log.parent.mkdir(parents=True)
    log.write_text(
        '{"kind":"entry","type":"user","content":"hello history"}\n'
        '{"kind":"entry","type":"assistant","content":"hi from the past"}\n',
        encoding="utf-8",
    )

    async def _run() -> None:
        app = SparkTui("current", str(ws))
        async with app.run_test(size=(100, 30)) as pilot:
            await app._run_command("resume", "/resume")
            await pilot.pause()
            picks = list(app.query(SessionPickRow))
            assert len(picks) == 1
            assert "hello history" in str(picks[0].content)
            assert "selected" in picks[0].classes
            picks[0].on_click()
            await pilot.pause()
            assert app.session_id == "cli-old"
            banners = [str(w.content) for w in app.query(UserBanner)]
            assert any("hello history" in b for b in banners), banners
            prose = "\n".join(str(w._src) for w in app.query(Prose))
            assert "hi from the past" in prose

    asyncio.run(_run())


def test_resume_escape_restores_session(tmp_path, monkeypatch):
    from agent_loop.cli.tui import ResumePicker, UserBanner
    from agent_loop.paths import session_log_path_for

    home = tmp_path / "home"
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    log = session_log_path_for(ws, "stay-here", home=home)
    log.parent.mkdir(parents=True)
    log.write_text(
        '{"kind":"entry","type":"user","content":"keep this chat"}\n',
        encoding="utf-8",
    )
    other = session_log_path_for(ws, "other-one", home=home)
    other.parent.mkdir(parents=True)
    other.write_text(
        '{"kind":"entry","type":"user","content":"another"}\n',
        encoding="utf-8",
    )

    async def _run() -> None:
        app = SparkTui("stay-here", str(ws))
        async with app.run_test(size=(100, 30)) as pilot:
            await app._run_command("resume", "/resume")
            await pilot.pause()
            assert list(app.query(ResumePicker))
            await app._resume_cancel()
            await pilot.pause()
            assert not list(app.query(ResumePicker))
            assert app.session_id == "stay-here"
            banners = [str(w.content) for w in app.query(UserBanner)]
            assert any("keep this chat" in b for b in banners), banners

    asyncio.run(_run())


def test_resume_keyboard_opens_selected(tmp_path, monkeypatch):
    from agent_loop.cli.tui import ResumePicker, SessionPickRow
    from agent_loop.paths import session_log_path_for

    home = tmp_path / "home"
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    for i, sid in enumerate(("first-a", "second-b")):
        path = session_log_path_for(ws, sid, home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'{{"kind":"entry","type":"user","content":"{sid} title"}}\n',
            encoding="utf-8",
        )
        import os
        import time

        os.utime(path, (time.time() + i, time.time() + i))

    async def _run() -> None:
        app = SparkTui("current", str(ws))
        async with app.run_test(size=(100, 30)) as pilot:
            await app._run_command("resume", "/resume")
            await pilot.pause()
            picker = app.query_one(ResumePicker)
            picker.focus()
            await pilot.pause()
            picker.action_cursor_down()
            await pilot.pause()
            picks = list(app.query(SessionPickRow))
            assert "selected" in picks[1].classes
            picker.action_open_selected()
            await pilot.pause()
            assert app.session_id == "first-a"

    asyncio.run(_run())


def test_resume_pages_and_click(tmp_path, monkeypatch):
    from agent_loop.cli.tui import ResumePageRow, SessionPickRow
    from agent_loop.paths import session_log_path_for

    home = tmp_path / "home"
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(12):
        path = session_log_path_for(ws, f"s{i:02d}", home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'{{"kind":"entry","type":"user","content":"q{i}"}}\n',
            encoding="utf-8",
        )
        import os
        import time

        os.utime(path, (time.time() + i, time.time() + i))

    async def _run() -> None:
        app = SparkTui("current", str(ws))
        async with app.run_test(size=(100, 30)) as pilot:
            await app._run_command("resume", "/resume")
            await pilot.pause()
            assert len(list(app.query(SessionPickRow))) == 10
            nav = list(app.query(ResumePageRow))
            assert nav and nav[0].delta == 1
            nav[0].on_click()
            await pilot.pause()
            rest = list(app.query(SessionPickRow))
            assert len(rest) == 2
            sid = rest[0].session_id
            rest[0].on_click()
            await pilot.pause()
            assert app.session_id == sid

    asyncio.run(_run())


def test_thinking_then_thought():
    asyncio.run(_run())


def test_prose_key_text_is_blue():
    from agent_loop.cli.tui import _markdown_as_text

    rendered = _markdown_as_text(
        "**今晚不发射** 见 [`loop.py`](https://example.com)\n\n### 三个情景\n\n普通句子",
        80,
    )
    assert "今晚不发射" in rendered.plain
    assert "loop.py" in rendered.plain
    bold_start = rendered.plain.find("今晚不发射")
    code_start = rendered.plain.find("loop.py")
    blue = (47, 100, 210)

    def _color_at(index: int):
        for span in rendered.spans:
            if span.start <= index < span.end and span.style and span.style.color:
                return span.style.color.triplet
        return None

    assert _color_at(bold_start) != blue
    assert _color_at(code_start) == blue


def test_prose_list_is_not_double_spaced():
    from agent_loop.cli.tui import _markdown_as_text

    rendered = _markdown_as_text("- 第一点\n- 第二点\n", 80)
    plain = rendered.plain
    i = plain.find("第一点")
    j = plain.find("第二点")
    between = plain[i:j]
    assert "第一点" in plain and "第二点" in plain
    assert between.count("\n\n") == 0


def test_scroll_follow_stays_off_when_user_scrolled():
    async def _run() -> None:
        app = SparkTui("follow-test", "/tmp/spark-agent-tui-follow")
        async with app.run_test(size=(80, 24)) as pilot:
            app._start_turn("q")
            app._follow = False
            app._handle_event({"kind": "assistant_delta", "text": "hello "})
            app._handle_event({"kind": "assistant", "text": "hello world"})
            await pilot.pause()
            assert app._follow is False

    asyncio.run(_run())


def test_markdown_table_draws_box():
    from agent_loop.cli.tui import _markdown_as_text

    src = (
        "| 情景 | 概率 | SOXL |\n"
        "| --- | --- | --- |\n"
        "| 反抽 | 45% | +5% |\n"
        "| 避险 | 35% | -5% |\n"
    )
    text = str(_markdown_as_text(src, 80))
    assert "┌" in text and "┐" in text
    assert "│" in text
    assert text.count("├") >= 2
    assert "反抽" in text
    assert "45%" in text


def test_format_elapsed():
    from agent_loop.cli.tui import format_elapsed, format_token_short

    assert format_elapsed(14) == "14s"
    assert format_elapsed(379) == "6m19s"
    assert format_elapsed(3723) == "1h2m3s"
    assert format_elapsed(0.4) == "0s"
    assert format_elapsed(0.6) == "1s"
    assert format_token_short(112_000) == "112K"
    assert format_token_short(1_000_000) == "1M"
    assert format_token_short(1_100_000) == "1.1M"


async def _finish() -> None:
    app = SparkTui("worked-test", "/tmp/spark-agent-tui-worked")
    async with app.run_test(size=(100, 30)) as pilot:
        app._busy = True
        app._busy_t0 = __import__("time").monotonic() - 379
        app._start_turn("hello")
        app._handle_event({"kind": "memory", "action": "flush", "result": "no_reply"})
        await pilot.pause()
        rows = [str(w.content) for w in app.query(TimelineRow)]
        assert not any("memory" in r or "flush" in r for r in rows), rows
        app._finish_turn()
        await pilot.pause()
        rows = [str(w.content) for w in app.query(TimelineRow)]
        assert any("Worked for 6m19s" in r for r in rows), rows
        assert "Worked for" not in _status(app), _status(app)


def test_worked_for_at_end_hides_memory_flush():
    asyncio.run(_finish())


async def _summary() -> None:
    app = SparkTui("summary-test", "/tmp/spark-agent-tui-summary")
    async with app.run_test(size=(100, 30)) as pilot:
        app._busy = True
        app._busy_t0 = __import__("time").monotonic() - 101
        app._start_turn("hello")
        app._handle_event({"kind": "context", "used": 112_000, "limit": 1_000_000})
        app._handle_event(
            {"kind": "compaction", "level": "microcompact", "before": 0.81, "after": 0.42}
        )
        await pilot.pause()
        mid = [str(w.content) for w in app.query(TimelineRow)]
        assert not any("compacted" in r for r in mid), mid
        app._finish_turn()
        await pilot.pause()
        rows = [str(w.content) for w in app.query(TimelineRow)]
        blob = "\n".join(rows)
        want = "Worked for 1m41s | deepseek-v4-flash | 112K / 1M | compacted microcompact 81% -> 42%"
        assert want in blob, blob
        assert want not in _status(app), _status(app)


def test_worked_summary_includes_model_context_compaction():
    asyncio.run(_summary())


async def _copy_prose() -> None:
    from agent_loop.cli.tui import Prose

    app = SparkTui("copy-test", "/tmp/spark-agent-tui-copy")
    async with app.run_test(size=(80, 24)) as pilot:
        turn = app._start_turn("q")
        prose = Prose("hello **world**\n\nsecond paragraph")
        turn.mount(prose)
        await pilot.pause()
        prose.text_select_all()
        await pilot.pause()
        text = app.screen.get_selected_text() or ""
        assert "hello" in text, repr(text)
        assert "world" in text, repr(text)
        assert "second paragraph" in text, repr(text)


def test_prose_can_copy_selection():
    asyncio.run(_copy_prose())


def test_thought_text_stays_off_the_timeline():
    async def _run() -> None:
        app = SparkTui("copy-thought", "/tmp/spark-agent-tui-copy-thought")
        async with app.run_test(size=(80, 24)) as pilot:
            app._start_turn("q")
            app._handle_event(
                {
                    "kind": "assistant_delta",
                    "text": "Chrome 已经连上，而且有一个已登录的 Gmail 标签。",
                    "channel": "reasoning",
                }
            )
            app._handle_event({"kind": "assistant_delta", "text": "ok", "channel": "content"})
            await pilot.pause()
            assert list(app.query(ThoughtBody)) == []
            rows = _thoughts(app)
            assert len(rows) == 1
            assert "Thought" in rows[0]
            assert "Gmail" not in rows[0]

    asyncio.run(_run())


def test_copy_command_copies_latest_reply_not_thought():
    async def _run() -> None:
        previous = subprocess.check_output(["pbpaste"])
        app = SparkTui("copy-cmd", "/tmp/spark-agent-tui-copy-cmd")
        async with app.run_test(size=(80, 24)) as pilot:
            app._start_turn("q")
            app._handle_event(
                {
                    "kind": "assistant_delta",
                    "text": "thought should stay",
                    "channel": "reasoning",
                }
            )
            app._handle_event(
                {"kind": "assistant", "text": "the reply body"}
            )
            await pilot.pause()
            await app._run_command("copy", "/copy")
            pasted = subprocess.check_output(["pbpaste"])
            assert pasted == b"the reply body", pasted
            assert b"thought should stay" not in pasted
            rows = [str(w.content) for w in app.query(TimelineRow)]
            assert any("Copied to clipboard" in row and "14 chars, 1 line" in row for row in rows), rows
            saved = Path(os.environ["PERMANENT_HOME"]) / "last-copy.txt"
            assert saved.read_text(encoding="utf-8") == "the reply body"
        subprocess.run(["pbcopy"], input=previous, check=False)

    import subprocess

    asyncio.run(_run())


def test_copy_reaches_macos_pasteboard():
    async def _run() -> None:
        previous = subprocess.check_output(["pbpaste"])
        app = SparkTui("pbcopy-test", "/tmp/spark-agent-tui-pbcopy")
        async with app.run_test(size=(80, 24)) as pilot:
            app._start_turn("q")
            app._handle_event(
                {
                    "kind": "assistant_delta",
                    "text": "思考可以复制",
                    "channel": "reasoning",
                }
            )
            app._handle_event(
                {"kind": "assistant_delta", "text": "正文可以复制", "channel": "content"}
            )
            await pilot.pause()
            assert list(app.query(ThoughtBody)) == []
            prose = app.query_one(Prose)
            prose.text_select_all()
            await pilot.pause()
            app.copy_to_clipboard(app.screen.get_selected_text() or "")
            pasted = subprocess.check_output(["pbpaste"])
            assert "正文可以复制".encode() in pasted, pasted
        subprocess.run(["pbcopy"], input=previous, check=False)

    import subprocess

    from agent_loop.cli.tui import Prose

    asyncio.run(_run())


def test_browser_exec_diamond_blinks_and_counts_seconds():
    async def _run() -> None:
        app = SparkTui("run-tool", "/tmp/spark-agent-tui-run-tool")
        async with app.run_test(size=(100, 24)) as pilot:
            app._start_turn("q")
            app._handle_event(
                {
                    "kind": "tool_start",
                    "name": "browser_exec",
                    "args": {"code": "print(page_info())"},
                }
            )
            await pilot.pause()
            from agent_loop.cli.tui import RunningToolRow

            row = app.query_one(RunningToolRow)
            mark = row.query_one(WaitingMark)
            assert mark._timer is not None
            assert mark._hollow is True
            text = str(row.query_one(".run-text").content)
            assert "browser_exec" in text
            assert "page_info" in text
            assert "0s" in text, text
            first = mark._on
            await pilot.pause(0.5)
            assert mark._on is not first
            row._t0 = time.monotonic() - 65
            await pilot.pause(0.6)
            text = str(row.query_one(".run-text").content)
            assert "1m5s" in text, text
            app._handle_event({"kind": "tool_result", "name": "browser_exec", "is_error": False})
            await pilot.pause()
            assert mark._timer is None
            assert mark._frozen is True
            text = str(row.query_one(".run-text").content)
            assert "1m5s" in text, text
            held = mark._on
            await pilot.pause(0.5)
            assert mark._on is held
            assert "◇" not in str(mark.render())

    asyncio.run(_run())


def test_fast_tool_drops_zero_seconds_when_done():
    async def _run() -> None:
        app = SparkTui("fast-tool", "/tmp/spark-agent-tui-fast-tool")
        async with app.run_test(size=(80, 24)) as pilot:
            app._start_turn("q")
            app._handle_event({"kind": "tool_start", "name": "read", "args": {"path": "a.py"}})
            await pilot.pause()
            from agent_loop.cli.tui import RunningToolRow

            row = app.query_one(RunningToolRow)
            assert "0s" in str(row.query_one(".run-text").content)
            app._handle_event({"kind": "tool_result", "name": "read", "is_error": False})
            await pilot.pause()
            text = str(row.query_one(".run-text").content)
            assert "Read" in text and "a.py" in text
            assert "0s" not in text, text
            assert row.query_one(WaitingMark)._timer is None

    asyncio.run(_run())


def test_empty_flash_thought_drops_and_next_thought_stays_separate():
    async def _run() -> None:
        app = SparkTui("thought-split", "/tmp/spark-agent-tui-thought-split")
        async with app.run_test(size=(100, 30)) as pilot:
            app._start_turn("q")
            app._handle_event({"kind": "assistant_delta", "text": "   ", "channel": "reasoning"})
            await pilot.pause()
            assert _thoughts(app), _thoughts(app)
            app._handle_event({"kind": "tool_start", "name": "read", "args": {"path": "a.py"}})
            await pilot.pause()
            assert _thoughts(app) == [], _thoughts(app)
            assert list(app.query(ThoughtBody)) == []
            app._handle_event({"kind": "tool_result", "name": "read", "is_error": False})
            app._handle_event(
                {"kind": "assistant_delta", "text": "first real thought", "channel": "reasoning"}
            )
            app._handle_event({"kind": "assistant_delta", "text": "ok", "channel": "content"})
            await pilot.pause()
            app._handle_event(
                {"kind": "assistant_delta", "text": "second thought", "channel": "reasoning"}
            )
            app._handle_event({"kind": "assistant", "text": "done"})
            await pilot.pause()
            assert list(app.query(ThoughtBody)) == []
            thoughts = _thoughts(app)
            assert len(thoughts) == 2
            assert all("first real thought" not in row and "second thought" not in row for row in thoughts)

    asyncio.run(_run())


def test_waiting_notice_diamond_blinks_until_tool_returns():
    async def _run() -> None:
        app = SparkTui("wait-notice", "/tmp/spark-agent-tui-wait")
        async with app.run_test(size=(80, 24)) as pilot:
            app._start_turn("q")
            app._handle_event({"kind": "notice", "text": "请点 Allow"})
            await pilot.pause()
            row = app.query_one(WaitingNotice)
            mark = row.query_one(WaitingMark)
            assert mark._timer is not None
            first = mark._on
            await pilot.pause(0.5)
            assert mark._on is not first
            app._handle_event({"kind": "tool_result", "name": "browser_exec", "is_error": False})
            await pilot.pause()
            assert mark._timer is None
            assert mark._on is True

    asyncio.run(_run())


def test_click_prompt_border_focuses_input():
    async def _run() -> None:
        app = SparkTui("prompt-edge", "/tmp/spark-agent-tui-prompt-edge")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one("#timeline").focus()
            await pilot.pause()
            assert app.focused.id != "prompt"
            wrap = app.query_one("#prompt-wrap")
            assert wrap.region.x == 2
            assert wrap.region.x + wrap.region.width == 78
            assert app.size.height - (wrap.region.y + wrap.region.height) == 1
            hit = await pilot.click("#prompt-wrap", offset=(3, 0))
            await pilot.pause()
            assert hit, app.get_widget_at(*wrap.region.offset)
            assert app.focused.id == "prompt", app.focused
            await pilot.click("#prompt-mark")
            await pilot.pause()
            assert app.focused.id == "prompt"

    asyncio.run(_run())


def test_clip_thought_uses_ellipsis():
    short = "let me think"
    assert _clip_thought(short) == short
    long_lines = "\n".join(f"line {i} " + ("x" * 40) for i in range(20))
    shown = _clip_thought(long_lines)
    assert shown.endswith("...")
    assert "line 19" not in shown
    assert "line 0" in shown


def test_busy_submit_queues_until_send_now_or_turn_ends():
    async def _run() -> None:
        from textual.widgets import Input

        from agent_loop.cli.tui import QueueAction, QueuedMessage, UserBanner

        app = SparkTui("queue-msg", "/tmp/spark-agent-tui-queue")
        async with app.run_test(size=(90, 24)) as pilot:
            app._start_turn("需要")
            app._busy = True
            prompt = app.query_one("#prompt", Input)
            prompt.value = "今天北京天气咋样"
            await app.on_input_submitted(Input.Submitted(prompt, prompt.value))
            await pilot.pause()
            row = app.query_one(QueuedMessage)
            assert "#1" in str(row.query_one(".q-text").content)
            assert "今天北京天气咋样" in str(row.query_one(".q-text").content)
            assert [act.action for act in row.query(QueueAction)] == ["send", "edit", "cancel"]
            assert app.loop.inbox.drain_steer() == []
            assert len(app.loop.inbox._follow_up) == 1
            banners = [str(b.content) for b in app.query(UserBanner)]
            assert banners.count("›  今天北京天气咋样") == 0

            app.send_queued_now(row.qid)
            await pilot.pause()
            assert list(app.query(QueuedMessage)) == []
            assert app.loop.inbox.drain_follow_up() == []
            steered = app.loop.inbox.drain_steer()
            assert steered and steered[0][0] == "今天北京天气咋样"
            banners = [str(b.content) for b in app.query(UserBanner)]
            assert banners.count("›  今天北京天气咋样") == 1
            app._handle_event({"kind": "user", "text": "今天北京天气咋样"})
            await pilot.pause()
            banners = [str(b.content) for b in app.query(UserBanner)]
            assert banners.count("›  今天北京天气咋样") == 1

            app._busy = True
            prompt.value = "再问一句"
            await app.on_input_submitted(Input.Submitted(prompt, prompt.value))
            await pilot.pause()
            queued = app.query_one(QueuedMessage)
            app.edit_queued(queued.qid)
            await pilot.pause()
            assert list(app.query(QueuedMessage)) == []
            assert app.query_one("#prompt", Input).value == "再问一句"
            assert app.loop.inbox.drain_follow_up() == []

            prompt.value = "丢掉"
            await app.on_input_submitted(Input.Submitted(prompt, prompt.value))
            await pilot.pause()
            queued = app.query_one(QueuedMessage)
            app.cancel_queued(queued.qid)
            await pilot.pause()
            assert list(app.query(QueuedMessage)) == []
            assert app.loop.inbox.drain_follow_up() == []

    asyncio.run(_run())
