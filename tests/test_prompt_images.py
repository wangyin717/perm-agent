"""用户贴图：整段绝对路径才当 drop，散文里的路径不当图。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent_loop.inbox import UserInbox
from agent_loop.prompt_images import (
    chip_for_backspace,
    chip_for_delete,
    expand_range_to_chips,
    persist_image_file,
    snap_cursor_out_of_chip,
    try_read_dropped_paths,
)
from agent_loop.recover import entries_to_messages, user_api_message
from agent_loop.tools.read_tool import IMAGE_OMIT_NOTE
from tests.test_read_tool import _min_png


def test_dropped_absolute_png_is_image(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(_min_png())
    got = try_read_dropped_paths(str(png))
    assert got is not None
    assert got[0][0] == "image"
    assert got[0][1] == png.resolve()


def test_prose_with_image_path_is_not_a_drop(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(_min_png())
    assert try_read_dropped_paths(f"看看 {png} 这张图") is None
    assert try_read_dropped_paths("shot.png") is None
    assert try_read_dropped_paths("./shot.png") is None


def test_quoted_absolute_png(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(_min_png())
    got = try_read_dropped_paths(f"'{png}'")
    assert got is not None and got[0][0] == "image"


def test_path_with_spaces_quoted_unquoted_escaped(tmp_path):
    png = tmp_path / "截屏2026-09-18 17.17.35.png"
    png.write_bytes(_min_png())
    posix = str(png)
    escaped = posix.replace(" ", r"\ ")
    for raw in (posix, f"'{posix}'", f'"{posix}"', f"'{posix}'", escaped):
        got = try_read_dropped_paths(raw)
        assert got is not None, raw
        assert got[0][0] == "image", raw
        assert got[0][1] == png.resolve()


def test_file_url_absolute_png(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(_min_png())
    got = try_read_dropped_paths(png.as_uri())
    assert got is not None and got[0][0] == "image"


def test_two_absolute_pngs(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(_min_png())
    b.write_bytes(_min_png())
    got = try_read_dropped_paths(f"{a}\n{b}")
    assert got is not None
    assert [kind for kind, _ in got] == ["image", "image"]


def test_png_bytes_without_image_ext_is_not_a_chip(tmp_path):
    raw = tmp_path / "shot"
    raw.write_bytes(_min_png())
    got = try_read_dropped_paths(str(raw))
    assert got is not None
    assert got[0][0] == "text"


def test_png_ext_without_magic_is_not_a_chip(tmp_path):
    fake = tmp_path / "shot.png"
    fake.write_text("not an image", encoding="utf-8")
    got = try_read_dropped_paths(str(fake))
    assert got is not None
    assert got[0][0] == "text"


def test_existing_non_image_is_text_path(tmp_path):
    py = tmp_path / "a.py"
    py.write_text("x = 1\n", encoding="utf-8")
    got = try_read_dropped_paths(str(py))
    assert got is not None
    assert got[0][0] == "text"
    assert got[0][1] == py.resolve()


def test_persist_and_user_message_blocks(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(_min_png())
    session = tmp_path / "chat"
    dest, mime = persist_image_file(png, session)
    assert dest.is_file()
    assert "input/images" in str(dest)
    assert mime == "image/png"
    row = {
        "content": "看看 [Image #1]",
        "media": [{"kind": "image", "mime": mime, "path": str(dest)}],
    }
    msg = user_api_message(row, supports_images=True)
    assert msg["role"] == "user"
    assert msg["content"][0]["type"] == "text"
    assert msg["content"][1]["type"] == "image_url"
    assert msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    omitted = user_api_message(row, supports_images=False)
    assert isinstance(omitted["content"], str)
    assert IMAGE_OMIT_NOTE in omitted["content"]
    dumped = json.dumps(row)
    assert "base64" not in dumped
    assert str(dest) in dumped


def test_user_entry_projects_from_jsonl(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(_min_png())
    dest, mime = persist_image_file(png, tmp_path / "chat")
    rows = [
        {
            "kind": "entry",
            "type": "user",
            "seq": 1,
            "content": "看 [Image #1]",
            "media": [{"kind": "image", "mime": mime, "path": str(dest)}],
        }
    ]
    msgs = entries_to_messages(rows, supports_images=True)
    assert msgs[0]["role"] == "user"
    assert any(block.get("type") == "image_url" for block in msgs[0]["content"])


def test_chip_is_atomic_span():
    text = "看 [Image #1] 和 [Image #2]"
    assert chip_for_backspace(text, text.index("]") + 1) == (
        text.index("["),
        text.index("]") + 1,
    )
    inside = text.index("Image")
    assert chip_for_delete(text, inside) == (text.index("["), text.index("]") + 1)
    assert chip_for_backspace(text, 0) is None
    snapped = snap_cursor_out_of_chip(text, inside)
    assert snapped in (text.index("["), text.index("]") + 1)
    lo, hi = expand_range_to_chips(text, inside, inside + 2)
    assert text[lo:hi] == "[Image #1]"


def test_inbox_steer_keeps_media():
    box = UserInbox()
    media = [{"kind": "image", "mime": "image/png", "path": "/tmp/x.png"}]
    box.push_steer("see [Image #1]", media)
    assert box.drain_steer() == [("see [Image #1]", media)]


def test_tui_paste_absolute_png_inserts_chip(tmp_path, monkeypatch):
    from agent_loop.cli.tui import SparkTui

    home = tmp_path / "home"
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    png = tmp_path / "shot.png"
    png.write_bytes(_min_png())
    notes = tmp_path / "notes.py"
    notes.write_text("x = 1\n", encoding="utf-8")

    async def _run() -> None:
        app = SparkTui("paste-img", str(ws))
        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt")
            prompt.focus()
            assert not app.attach_paste_text(f"看看 {png}")
            assert app.attach_paste_text(str(png))
            await pilot.pause()
            assert "[Image #1]" in prompt.value
            assert len(app._draft_images) == 1
            dest = Path(app._draft_images[0]["path"])
            assert dest.is_file()
            assert "input/images" in str(dest)
            assert dest.read_bytes() == png.read_bytes()
            assert app.attach_paste_text(str(notes))
            await pilot.pause()
            assert str(notes.resolve()) in prompt.value
            media = app._take_submit_media(prompt.value)
            assert len(media) == 1
            assert media[0]["path"] == str(dest)

    asyncio.run(_run())


def test_tui_typed_quoted_path_becomes_chip(tmp_path, monkeypatch):
    """拖文件进终端通常不是 Paste，只是把带引号的路径写进输入框。"""
    from agent_loop.cli.tui import SparkTui

    home = tmp_path / "home"
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    png = tmp_path / "截屏2026-09-18 17.17.35.png"
    png.write_bytes(_min_png())

    async def _run() -> None:
        app = SparkTui("drop-img", str(ws))
        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt")
            prompt.focus()
            prompt.value = f"'{png}'"
            await pilot.pause()
            assert "[Image #1]" in prompt.value
            assert str(png) not in prompt.value
            assert len(app._draft_images) == 1

    asyncio.run(_run())


def test_tui_backspace_deletes_whole_chip(tmp_path, monkeypatch):
    from agent_loop.cli.tui import SparkTui

    home = tmp_path / "home"
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    png = tmp_path / "shot.png"
    png.write_bytes(_min_png())

    async def _run() -> None:
        app = SparkTui("chip-del", str(ws))
        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt")
            prompt.focus()
            assert app.attach_paste_text(str(png))
            await pilot.pause()
            assert prompt.value.strip() == "[Image #1]"
            assert app._draft_images
            prompt.action_end()
            prompt.action_delete_left()
            await pilot.pause()
            assert "[Image #1]" not in prompt.value
            assert "Image" not in prompt.value
            assert app._draft_images == []

            assert app.attach_paste_text(str(png))
            await pilot.pause()
            prompt.action_home()
            prompt.action_delete_right()
            await pilot.pause()
            assert "[Image #1]" not in prompt.value
            assert app._draft_images == []

    asyncio.run(_run())
