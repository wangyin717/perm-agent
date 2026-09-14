"""TUI：思考计时在底栏整轮都在，时间线先 Thinking 再落成 Thought。"""
from __future__ import annotations

import asyncio

from agent_loop.cli.tui import SparkTui, TimelineRow


def _status(app: SparkTui) -> str:
    return str(app.query_one("#status").content)


def _thoughts(app: SparkTui) -> list[str]:
    return [str(w.content) for w in app.query(TimelineRow) if "thought" in w.classes]


async def _run() -> None:
    app = SparkTui("think-test", "/tmp/spark-agent-tui-think")
    async with app.run_test(size=(100, 30)) as pilot:
        app._busy = True
        app._busy_t0 = __import__("time").monotonic()
        app._start_turn("hello")
        await pilot.pause()
        assert "Thinking" in _status(app), _status(app)
        live = _thoughts(app)
        assert len(live) == 1
        assert "Thinking" in live[0]
        assert "Thought" not in live[0]

        app._handle_event({"kind": "tools", "tool_calls": [{"name": "read"}]})
        await pilot.pause()
        assert "Thinking" in _status(app), _status(app)
        done = _thoughts(app)
        assert len(done) == 1
        assert "Thought" in done[0]
        assert "for" in done[0]

        app._handle_event({"kind": "tool_start", "name": "read", "args": {"path": "a.py"}})
        await asyncio.sleep(0.15)
        await pilot.pause()
        assert "Thinking" in _status(app), _status(app)

        app._handle_event({"kind": "tool_result", "name": "read", "is_error": False})
        await pilot.pause()
        rows = _thoughts(app)
        assert len(rows) == 2
        assert "Thinking" in rows[-1]
        app._end_think()
        await pilot.pause()
        rows = _thoughts(app)
        assert all("Thought" in r for r in rows)
        assert len(rows) == 2


def test_thinking_footer_survives_tools():
    asyncio.run(_run())


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
        app._start_turn("hello", think=False)
        app._handle_event({"kind": "memory", "action": "flush", "result": "no_reply"})
        await pilot.pause()
        rows = [str(w.content) for w in app.query(TimelineRow)]
        assert not any("memory" in r or "flush" in r for r in rows), rows
        app._finish_turn()
        await pilot.pause()
        rows = [str(w.content) for w in app.query(TimelineRow)]
        assert any("Worked for 6m19s" in r for r in rows), rows
        assert "Worked for 6m19s" in _status(app)


def test_worked_for_at_end_hides_memory_flush():
    asyncio.run(_finish())


async def _summary() -> None:
    app = SparkTui("summary-test", "/tmp/spark-agent-tui-summary")
    async with app.run_test(size=(100, 30)) as pilot:
        app._busy = True
        app._busy_t0 = __import__("time").monotonic() - 101
        app._start_turn("hello", think=False)
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
        assert want in _status(app)


def test_worked_summary_includes_model_context_compaction():
    asyncio.run(_summary())


async def _copy_prose() -> None:
    from agent_loop.cli.tui import Prose

    app = SparkTui("copy-test", "/tmp/spark-agent-tui-copy")
    async with app.run_test(size=(80, 24)) as pilot:
        turn = app._start_turn("q", think=False)
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
