"""会话显示名 title.json 的读写与自动标题。"""
from __future__ import annotations

import json

from agent_loop.session_title import (
    AUTO_MAX,
    MANUAL_MAX,
    clip_title,
    display_title,
    ensure_auto_title,
    read_title,
    reset_auto_title,
    set_manual_title,
    title_path,
    write_title,
)


def _write_log(tmp_path, session_id: str, first_user: str) -> None:
    log = title_path(tmp_path, session_id).parent / "session.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    row = {"kind": "entry", "type": "user", "content": first_user}
    log.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")


def test_clip_title_collapses_whitespace_and_drops_urls():
    assert clip_title("  去   看 一下  ") == "去 看 一下"
    assert clip_title("看 https://a.b/c?q=1 这个") == "看 这个"
    assert clip_title("https://only.url/x", limit=10) == "https://only.url/x"[:9] + "…"


def test_clip_title_ellipsis_respects_limit():
    text = "字" * 80
    out = clip_title(text, limit=AUTO_MAX)
    assert len(out) == AUTO_MAX
    assert out.endswith("…")


def test_read_title_missing_and_broken_file(tmp_path):
    # 不存在 → 空标题
    empty = read_title(tmp_path, "s1")
    assert empty == {"title": "", "title_is_manual": False}
    # 坏 JSON → 空标题
    path = title_path(tmp_path, "s2")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert read_title(tmp_path, "s2") == {"title": "", "title_is_manual": False}


def test_write_title_clips_and_persists(tmp_path):
    payload = write_title(tmp_path, "s1", "字" * 200, manual=False)
    assert payload["title"] == "字" * (AUTO_MAX - 1) + "…"
    assert payload["title_is_manual"] is False
    assert json.loads(title_path(tmp_path, "s1").read_text(encoding="utf-8")) == payload


def test_manual_title_allows_longer_limit(tmp_path):
    set_manual_title(tmp_path, "s1", "字" * 90)
    assert len(read_title(tmp_path, "s1")["title"]) == 90
    assert read_title(tmp_path, "s1")["title_is_manual"] is True


def test_display_title_fallback_chain(tmp_path):
    assert display_title(tmp_path, "s1", preview="预览") == "预览"
    assert display_title(tmp_path, "s1") == "s1"
    write_title(tmp_path, "s1", "我的标题", manual=True)
    assert display_title(tmp_path, "s1", preview="预览") == "我的标题"


def test_ensure_auto_title_seeds_from_first_user_message(tmp_path):
    _write_log(tmp_path, "s1", "帮我看看这个报错 https://example.com/a very long " + "x" * 120)
    meta = ensure_auto_title(tmp_path, "s1")
    assert meta["title_is_manual"] is False
    assert len(meta["title"]) == AUTO_MAX
    assert meta["title"].endswith("…")
    assert "https://" not in meta["title"]


def test_ensure_auto_title_never_touches_manual(tmp_path):
    set_manual_title(tmp_path, "s1", "手动名字")
    _write_log(tmp_path, "s1", "日志里的第一句话")
    assert ensure_auto_title(tmp_path, "s1")["title"] == "手动名字"


def test_ensure_auto_title_keeps_existing_normal_title(tmp_path):
    write_title(tmp_path, "s1", "已有标题", manual=False)
    _write_log(tmp_path, "s1", "新的第一句话")
    assert ensure_auto_title(tmp_path, "s1")["title"] == "已有标题"


def test_ensure_auto_title_overrides_url_only_title(tmp_path):
    write_title(tmp_path, "s1", "https://example.com/only", manual=False)
    _write_log(tmp_path, "s1", "真正想显示的一句话")
    assert ensure_auto_title(tmp_path, "s1")["title"] == "真正想显示的一句话"


def test_ensure_auto_title_no_log_uses_hint(tmp_path):
    meta = ensure_auto_title(tmp_path, "s1", hint="提示语")
    assert meta["title"] == "提示语"


def test_reset_auto_title_ignores_manual(tmp_path):
    set_manual_title(tmp_path, "s1", "手动名字")
    _write_log(tmp_path, "s1", "第一句话")
    meta = reset_auto_title(tmp_path, "s1")
    assert meta["title_is_manual"] is False
    assert meta["title"].startswith("第一句话")
    assert len(meta["title"]) <= MANUAL_MAX
