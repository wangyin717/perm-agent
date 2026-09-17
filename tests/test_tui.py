"""TUI：流思考在时间线先 Thinking 再落成 Thought。"""
from __future__ import annotations

import asyncio
import time

from agent_loop.cli.tui import SparkTui, TimelineRow


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
        await pilot.pause()
        live = _thoughts(app)
        assert len(live) == 1, live
        assert "Thinking" in live[0]
        assert "Thought" not in live[0]
        assert "Waiting" not in _status(app), _status(app)

        app._handle_event({"kind": "assistant_delta", "text": "hello", "channel": "content"})
        await pilot.pause()
        done = _thoughts(app)
        assert len(done) == 1
        assert "Thought" in done[0]
        assert "for" in done[0]

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


def test_markdown_as_text_cache_returns_copy():
    from agent_loop.cli import tui as tui_mod

    tui_mod._MD_CACHE.clear()
    src = "**hello** `x.py`"
    a = tui_mod._markdown_as_text(src, 40)
    b = tui_mod._markdown_as_text(src, 40)
    assert a.plain == b.plain
    assert a is not b
    assert len(tui_mod._MD_CACHE) == 1


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
    blue = (26, 115, 232)

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
