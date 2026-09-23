"""Grok 风格时间线：用户顶栏、◆ 工具行、中间穿插 markdown。"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich import box
from rich.console import Console, ConsoleOptions, NewLine, RenderResult
from rich.markdown import Heading, Markdown as RichMarkdown, TableElement
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from textual.app import App, ComposeResult
from textual.actions import SkipAction
from textual.binding import Binding
from pygments.token import Token
from textual.containers import Horizontal, Vertical, VerticalGroup, VerticalScroll
from textual.markup import escape as markup_escape
from textual.selection import Selection
from textual.visual import RenderOptions
from textual.events import Paste, TextSelected
from textual.widgets import Input, Static

from agent_loop import events
from agent_loop.cli.app import CliDeps
from agent_loop.loop import ReactAgentLoop
from agent_loop.paths import spark_home
from agent_loop.tools.diff_view import clip_diff_line
from agent_loop.paths import list_sessions, session_log_path_for
from agent_loop.session_title import (
    ensure_auto_title,
    reset_auto_title,
    set_manual_title,
)
from agent_loop.session_log import SessionLog
from agent_loop.prompt_images import (
    chip_for_backspace,
    chip_for_delete,
    expand_range_to_chips,
    next_image_number,
    persist_image_bytes,
    persist_image_file,
    placeholders_in_text,
    read_clipboard_image_bytes,
    snap_cursor_out_of_chip,
    try_read_dropped_paths,
)

HELP = """commands:
  /help      this list
  /copy      copy the latest reply (not the thought)
  /exit      quit (/quit same)
  /new       new session (blank history)
  /resume    list sessions (click a row; n/p or ←/→ to page)
  /rename    set session title (`/rename --auto` uses first prompt)
  /session   show session id and log path
keys: Enter send  Esc abort  Ctrl+Q quit"""


_IDLE = "idle  /help  Enter send  Esc abort  Ctrl+Q quit"
_ARG_MAX = 64
_STEP_TOOLS_MAX = 3
_RESUME_PAGE = 10


def parse_slash(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None
    name = raw.split(None, 1)[0].lower()
    if name in ("/exit", "/quit"):
        return "exit"
    if name == "/new":
        return "new"
    if name == "/help":
        return "help"
    if name == "/copy":
        return "copy"
    if name == "/session":
        return "session"
    if name == "/resume":
        return "resume"
    if name in ("/rename", "/title"):
        return "rename"
    return None


SLASH_COMMANDS = (
    ("/help", "this list", False),
    ("/copy", "copy the latest reply", False),
    ("/new", "new session", False),
    ("/resume", "list sessions", False),
    ("/rename", "set session title", True),
    ("/session", "show session id and path", False),
    ("/exit", "quit", False),
    ("/quit", "quit", False),
)


def slash_prefix(text: str) -> Optional[str]:
    raw = text or ""
    if not raw.startswith("/") or " " in raw or "\n" in raw:
        return None
    return raw.lower()


def matching_slash(prefix: str) -> List[Tuple[str, str, bool]]:
    if prefix == "/":
        return list(SLASH_COMMANDS)
    return [item for item in SLASH_COMMANDS if item[0].startswith(prefix)]


def _basename(path: str) -> str:
    return Path(path or "").name or path or ""


def format_grok_tool(name: str, args: Optional[Dict] = None) -> str:
    args = args or {}
    if name == "read":
        extra = ""
        offset, limit = args.get("offset"), args.get("limit")
        if offset is not None or limit is not None:
            start = int(offset) if offset is not None else 1
            extra = f" ({start}-)" if limit is None else f" ({start}-{start + max(int(limit), 0) - 1})"
        return f"Read  {_basename(str(args.get('path') or ''))}{extra}"
    if name == "grep":
        pat = str(args.get("pattern") or "")
        return f'Search  "{pat}"' if pat else "Search"
    if name == "web_search":
        q = str(args.get("query") or "")
        return f'Search  "{q}"' if q else "Search"
    if name == "web_fetch":
        return f"Fetch  {args.get('url') or ''}"
    if name == "write":
        return f"Write  {_basename(str(args.get('path') or ''))}"
    if name == "edit":
        return f"Edit  {str(args.get('path') or '')}"
    if name == "bash":
        cmd = " ".join(str(args.get("cmd") or args.get("command") or "").split())
        return f"Bash  {cmd}" if cmd else "Bash"
    if name == "memory_search":
        q = str(args.get("query") or args.get("path") or "")
        return f'Memory  "{q}"' if q else "Memory"
    if name == "browser_screenshot":
        return "browser_screenshot"
    if name == "browser_exec":
        code = str(args.get("code") or "").strip().splitlines()
        first = " ".join((code[0] if code else "").split())
        return f"browser_exec  {first}" if first else "browser_exec"
    if name == "computer":
        tool = str(args.get("name") or "").strip()
        return f"computer  {tool}" if tool else "computer"
    return name


def _clip_arg(text: str, limit: int = _ARG_MAX) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


# GrokDay：正文 md_text #444444，标题/强调 #262626，弱化 #767676，链接 #2F64D2
# 页底和输入框同一浅灰，不要输入框单独一块白。
_BLUE = "#2F64D2"
_TEXT = "#262626"
_SECONDARY = "#444444"
_MUTED = "#767676"
_PAGE = "#f5f5f5"
_PROSE_THEME = Theme(
    {
        "markdown.h1": Style(bold=True, color=_TEXT),
        "markdown.h2": Style(bold=True, color=_TEXT),
        "markdown.h3": Style(bold=True, color=_TEXT),
        "markdown.h4": Style(bold=True, color=_TEXT),
        "markdown.h5": Style(bold=True, color=_TEXT),
        "markdown.h6": Style(color=_MUTED),
        "markdown.strong": Style(bold=True, color=_TEXT),
        "markdown.code": Style(bold=True, color=_BLUE),
        "markdown.code_block": Style(color=_BLUE),
        "markdown.link": Style(color=_BLUE, underline=True),
        "markdown.link_url": Style(color=_BLUE, underline=True),
        "markdown.paragraph": Style(color=_SECONDARY),
        "markdown.em": Style(italic=True, color=_SECONDARY),
        "markdown.list": Style(color=_SECONDARY),
        "markdown.item.number": Style(color=_SECONDARY),
        "markdown.block_quote": Style(color=_MUTED),
        "markdown.hr": Style(color=_MUTED),
        "markdown.table.border": Style(color=_MUTED),
        "markdown.table.header": Style(bold=True, color=_TEXT),
        "markdown.kbd": Style(bold=True, color=_BLUE),
    }
)


class _LeftHeading(Heading):
    LEVEL_ALIGN = {
        "h1": "left",
        "h2": "left",
        "h3": "left",
        "h4": "left",
        "h5": "left",
        "h6": "left",
    }

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield from super().__rich_console__(console, options)
        yield NewLine()


class _BoxedTable(TableElement):
    """Grok 正文表格：Unicode 方框 + 紧凑单元格，不要 Rich SIMPLE 那种稀散对齐。"""

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        table = Table(
            box=box.SQUARE,
            pad_edge=True,
            padding=(0, 1),
            collapse_padding=True,
            show_edge=True,
            show_header=True,
            show_lines=True,
            expand=False,
            style="markdown.table.border",
            border_style="markdown.table.border",
            header_style="markdown.table.header",
        )
        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                table.add_column(heading, overflow="fold", no_wrap=False)
        if self.body is not None:
            for row in self.body.rows:
                table.add_row(*[element.content for element in row.cells])
        yield table


class ProseMarkdown(RichMarkdown):
    """h1 左对齐；表格走方框。"""

    def __init__(self, markup: str) -> None:
        super().__init__(
            markup or " ",
            justify="left",
            code_theme="default",
            inline_code_theme="default",
        )
        self.elements = dict(RichMarkdown.elements)
        self.elements["heading_open"] = _LeftHeading
        self.elements["table_open"] = _BoxedTable


_MD_CACHE: Dict[Tuple[int, str], Text] = {}
_MD_CACHE_MAX = 24


def _markdown_as_text(src: str, width: int) -> Text:
    """Markdown 画成 Rich Text，Textual 才能拖选/复制。

    用 record + export_text(styles=True)：capture() 会丢掉 truecolor，标题/加粗就只剩粗体。
    """
    key = (max(int(width), 8), src or " ")
    cached = _MD_CACHE.get(key)
    if cached is not None:
        return cached.copy()
    console = Console(
        file=StringIO(),
        width=key[0],
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        theme=_PROSE_THEME,
        legacy_windows=False,
        record=True,
    )
    console.print(ProseMarkdown(src or " "), end="")
    raw = console.export_text(styles=True).rstrip("\n") or " "
    rendered = Text.from_ansi(raw)
    if len(_MD_CACHE) >= _MD_CACHE_MAX:
        _MD_CACHE.pop(next(iter(_MD_CACHE)))
    _MD_CACHE[key] = rendered
    return rendered.copy()


def _bold_verb(text: str) -> str:
    """工具名加粗，入参灰色且截断。"""
    parts = text.split(None, 1)
    if len(parts) == 2:
        arg = markup_escape(_clip_arg(parts[1]))
        return f"[bold]{parts[0]}[/] [{_MUTED}]{arg}[/]"
    return f"[bold]{text}[/]"


def _chrome_label(workspace: str) -> str:
    """顶栏：分支和项目路径，家目录收成 ~。"""
    path = Path(workspace).expanduser()
    try:
        path = path.resolve()
    except OSError:
        pass
    try:
        home = Path.home().resolve()
    except OSError:
        home = Path.home()
    try:
        shown = "~/" + path.relative_to(home).as_posix()
    except ValueError:
        shown = str(path)
    branch = _git_branch(path)
    if branch:
        return f"{branch} {shown}"
    return shown


def _git_branch(workspace: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    name = (proc.stdout or "").strip()
    if not name or name == "HEAD":
        return ""
    return name


def _mark(text: str) -> str:
    """过程行：菱形和后面的字，跟正在跑的工具行同一列。"""
    return f"[{_MUTED}]◆[/] {text}"


def _thinking_line(dt: float) -> str:
    return _mark(f"[bold]Thinking[/][{_MUTED}]… {dt:.1f}s[/]")


def _thought_line(dt: float) -> str:
    return _mark(f"[bold]Thought[/] [{_MUTED}]for {dt:.1f}s[/]")


_THOUGHT_MAX_CHARS = 480
_THOUGHT_MAX_LINES = 6


def _clip_thought(text: str) -> str:
    raw = text or ""
    if not raw.strip():
        return ""
    lines = raw.splitlines()
    clipped = False
    if len(lines) > _THOUGHT_MAX_LINES:
        lines = lines[:_THOUGHT_MAX_LINES]
        clipped = True
    body = "\n".join(lines)
    if len(body) > _THOUGHT_MAX_CHARS:
        body = body[:_THOUGHT_MAX_CHARS].rstrip()
        clipped = True
    if clipped:
        body = body.rstrip() + "..."
    return body


def format_elapsed(seconds: float) -> str:
    total = max(int(round(seconds)), 0)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes}m{secs}s"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def format_token_short(n: int) -> str:
    n = max(int(n), 0)
    if n >= 1_000_000:
        val = n / 1_000_000
        if abs(val - round(val)) < 0.05:
            return f"{int(round(val))}M"
        return f"{val:.1f}M"
    return f"{int(round(n / 1000))}K"


def _muted(text: str) -> str:
    return f"[{_MUTED}]{text}[/]"


def _display_home_path(path: Path) -> str:
    try:
        rel = path.resolve().relative_to(Path.home())
    except ValueError:
        return str(path)
    return f"~/{rel.as_posix()}"


def _copy_notice(text: str, path: Path) -> str:
    n_chars = len(text)
    n_lines = len(text.splitlines()) or 1
    unit = "line" if n_lines == 1 else "lines"
    return (
        f"Copied to clipboard (also saved to {_display_home_path(path)}) "
        f"({n_chars} chars, {n_lines} {unit})"
    )


_PYGMENT_COLORS = {
    Token.Keyword: "#7c3aed",
    Token.Keyword.Constant: "#7c3aed",
    Token.Name.Builtin: "#7c3aed",
    Token.Name.Function: _TEXT,
    Token.Name.Class: _TEXT,
    Token.Name.Decorator: _BLUE,
    Token.String: "#C3691E",
    Token.String.Doc: "#C3691E",
    Token.Comment: _MUTED,
    Token.Number: _BLUE,
    Token.Operator: _SECONDARY,
}


def _pygment_style(tok) -> str:
    while tok is not None:
        if tok in _PYGMENT_COLORS:
            return _PYGMENT_COLORS[tok]
        tok = getattr(tok, "parent", None)
    return _TEXT


def highlight_code_line(code: str, filename: str = "") -> Text:
    code = clip_diff_line(code.replace("\n", ""))
    try:
        from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
        from pygments.util import ClassNotFound
    except ImportError:
        return Text(code, style=_TEXT)
    lexer = None
    if filename:
        try:
            lexer = get_lexer_for_filename(filename, code, stripnl=False)
        except (ClassNotFound, ValueError):
            lexer = None
    if lexer is None:
        try:
            lexer = get_lexer_by_name("python", stripnl=False)
        except ClassNotFound:
            return Text(code, style=_TEXT)
    out = Text()
    for tok, val in lexer.get_tokens(code + "\n"):
        val = val.replace("\n", "")
        if val:
            out.append(val, _pygment_style(tok))
    return out if out.plain else Text(code, style=_TEXT)


class UserBanner(Static):
    DEFAULT_CSS = """
    UserBanner {
        background: #dedede;
        color: #262626;
        padding: 1 2;
        width: 100%;
        margin-bottom: 1;
    }
    """


class QueueAction(Static):
    """排队条上的 Send now / edit / cancel。"""

    def __init__(self, action: str, label: str) -> None:
        super().__init__(label, markup=False)
        self.action = action

    def on_click(self) -> None:
        row = self.parent
        qid = getattr(row, "qid", "")
        send = getattr(self.app, "send_queued_now", None)
        edit = getattr(self.app, "edit_queued", None)
        cancel = getattr(self.app, "cancel_queued", None)
        if self.action == "send" and callable(send):
            send(qid)
        elif self.action == "edit" and callable(edit):
            edit(qid)
        elif self.action == "cancel" and callable(cancel):
            cancel(qid)


class QueuedMessage(Horizontal):
    """当前回合还在跑时先排着的下一句。"""

    DEFAULT_CSS = """
    QueuedMessage {
        height: 1;
        width: 100%;
        background: #dedede;
        color: #262626;
        padding: 0 2;
    }
    QueuedMessage .q-text {
        width: 1fr;
        height: 1;
        content-align: left middle;
    }
    QueuedMessage QueueAction {
        width: auto;
        height: 1;
        color: #2F64D2;
        content-align: right middle;
        padding: 0 0 0 1;
    }
    """

    def __init__(self, qid: str, text: str, index: int, media: Optional[List[Dict]] = None) -> None:
        super().__init__()
        self.qid = qid
        self._text = text
        self._media = list(media or [])
        self._index = index

    def compose(self) -> ComposeResult:
        yield Static("", classes="q-text", markup=False)
        yield QueueAction("send", "[Send now]")
        yield QueueAction("edit", "[edit]")
        yield QueueAction("cancel", "[cancel]")

    def on_mount(self) -> None:
        self.set_index(self._index)

    def set_index(self, index: int) -> None:
        self._index = index
        shown = " ".join((self._text or "").split())
        try:
            self.query_one(".q-text", Static).update(f"#{index}  {shown}")
        except Exception:
            pass


class TimelineRow(Static):
    DEFAULT_CSS = """
    TimelineRow {
        width: 100%;
        height: 1;
        margin: 0;
        padding: 0 2;
        color: #444444;
    }
    TimelineRow.thought { color: #444444; }
    TimelineRow.tool { color: #444444; }
    TimelineRow.edit { color: #444444; }
    TimelineRow.muted {
        height: auto;
        color: #767676;
    }
    TimelineRow.notice {
        height: auto;
        color: #2F64D2;
    }
    """


class WaitingMark(Static):
    """左边的菱形。等用户确认时蓝/灰交替；工具执行时实心/空心交替。"""

    def __init__(
        self,
        *,
        live: str = _BLUE,
        rest: str = "#c7c7cc",
        frozen_color: Optional[str] = None,
        hollow: bool = False,
    ) -> None:
        super().__init__("", markup=True)
        self._on = True
        self._timer = None
        self._live = live
        self._rest = rest
        self._frozen_color = frozen_color
        self._hollow = hollow
        self._frozen = False

    def on_mount(self) -> None:
        if getattr(self.parent, "_frozen", False):
            self._frozen = True
        self._paint()
        if self._frozen:
            return
        self._timer = self.set_interval(0.45, self._tick)

    def _tick(self) -> None:
        self._on = not self._on
        self._paint()

    def _paint(self) -> None:
        if self._frozen and self._frozen_color:
            color = self._frozen_color
            glyph = "◆"
        elif self._on:
            color = self._live
            glyph = "◆"
        else:
            color = self._rest
            glyph = "◇" if self._hollow else "◆"
        self.update(f"[{color}]{glyph}[/]")

    def freeze(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._frozen = True
        self._on = True
        self._paint()


class WaitingNotice(Horizontal):
    DEFAULT_CSS = """
    WaitingNotice {
        height: auto;
        width: 100%;
        padding: 0 2;
        background: #f5f5f5;
    }
    WaitingNotice WaitingMark {
        width: 2;
        height: 1;
    }
    WaitingNotice .wait-text {
        width: 1fr;
        height: auto;
        color: #2F64D2;
        background: #f5f5f5;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        yield WaitingMark()
        yield Static(self._text, classes="wait-text", markup=True)

    def freeze(self) -> None:
        try:
            self.query_one(WaitingMark).freeze()
        except Exception:
            pass


class RunningToolRow(Horizontal):
    """工具还在跑：菱形实心/空心闪，旁边是已经用了几秒。结束后菱形停住。"""

    DEFAULT_CSS = """
    RunningToolRow {
        height: 1;
        width: 100%;
        padding: 0 2;
        background: #f5f5f5;
    }
    RunningToolRow WaitingMark {
        width: 2;
        height: 1;
        background: #f5f5f5;
    }
    RunningToolRow .run-text {
        width: 1fr;
        height: 1;
        color: #444444;
        background: #f5f5f5;
    }
    """

    def __init__(self, name: str, args: Optional[Dict] = None) -> None:
        super().__init__()
        self._name = name
        self._args = dict(args or {})
        self._t0 = time.monotonic()
        self._shown = format_elapsed(0)
        self._frozen = False
        self._timer = None

    def _text(self, dt: float, *, final: bool) -> str:
        body = _bold_verb(format_grok_tool(self._name, self._args))
        if final and dt < 1.0:
            return body
        return f"{body}  [{_MUTED}]{format_elapsed(int(dt))}[/]"

    def compose(self) -> ComposeResult:
        yield WaitingMark(live=_MUTED, rest="#c7c7cc", frozen_color=_MUTED, hollow=True)
        dt = max(0.0, time.monotonic() - self._t0)
        yield Static(self._text(dt, final=self._frozen), classes="run-text", markup=True)

    def on_mount(self) -> None:
        if self._frozen:
            self._settle()
            return
        self._timer = self.set_interval(0.5, self._tick)

    def _tick(self) -> None:
        if self._frozen:
            return
        shown = format_elapsed(int(time.monotonic() - self._t0))
        if shown == self._shown:
            return
        self._shown = shown
        self._paint_text(final=False)

    def _paint_text(self, *, final: bool) -> None:
        dt = max(0.0, time.monotonic() - self._t0)
        try:
            self.query_one(".run-text", Static).update(self._text(dt, final=final))
        except Exception:
            pass

    def _settle(self) -> None:
        try:
            self.query_one(WaitingMark).freeze()
        except Exception:
            pass
        self._paint_text(final=True)

    def freeze(self) -> None:
        self._frozen = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._settle()


class ThoughtBody(Static):
    """思考正文：可拖选复制；过长只显示开头，以 ... 收住。"""

    ALLOW_SELECT = True
    DEFAULT_CSS = """
    ThoughtBody {
        height: auto;
        width: 1fr;
        margin: 0 2 1 2;
        padding: 0;
        color: #444444;
        background: #f5f5f5;
        pointer: text;
    }
    """

    def __init__(self, text: str = "") -> None:
        self._src = text or ""
        shown = _clip_thought(self._src)
        super().__init__(Text(shown, style="#444444"), shrink=True, markup=False)

    def set_text(self, text: str) -> None:
        self._src = text or ""
        self.update(Text(_clip_thought(self._src), style="#444444"))

    def get_selection(self, selection: Selection) -> Optional[Tuple[str, str]]:
        visual = self._render()
        width = max(int(self.content_size.width or self.size.width or 1), 1)
        try:
            strips = visual.render_strips(
                width,
                None,
                self.visual_style,
                RenderOptions(self._get_style, self.styles, None, None),
            )
            laid_out = "\n".join(strip.text for strip in strips)
        except Exception:
            laid_out = _clip_thought(self._src)
        extracted = selection.extract(laid_out)
        if extracted:
            return extracted, "\n"
        shown = _clip_thought(self._src)
        if shown.strip():
            return shown, "\n"
        return None


class SessionPickRow(Static):
    """可点的会话行。"""

    DEFAULT_CSS = """
    SessionPickRow {
        width: 100%;
        height: 1;
        margin: 0;
        padding: 0 2;
        color: #444444;
        background: #f5f5f5;
    }
    SessionPickRow:hover {
        background: #e8e8ea;
        color: #262626;
    }
    SessionPickRow.selected {
        background: #d8d8dc;
        color: #262626;
    }
    """

    def __init__(self, session_id: str, label: str) -> None:
        super().__init__(label, markup=False)
        self.session_id = session_id

    def on_click(self) -> None:
        switch = getattr(self.app, "_switch_session", None)
        if callable(switch):
            self.app.run_worker(switch(self.session_id, replay=True), exclusive=True, group="session")


class ResumePageRow(Static):
    DEFAULT_CSS = """
    ResumePageRow {
        width: 100%;
        height: 1;
        margin: 0;
        padding: 0 2;
        color: #767676;
        background: #f5f5f5;
    }
    ResumePageRow:hover {
        background: #e8e8ea;
        color: #262626;
    }
    """

    def __init__(self, delta: int, label: str) -> None:
        super().__init__(label, markup=False)
        self.delta = delta

    def on_click(self) -> None:
        turn = getattr(self.app, "_resume_turn_page", None)
        if callable(turn):
            turn(self.delta)


class ResumePicker(VerticalGroup, can_focus=True):
    """会话列表：↑↓ 选中，Enter 打开，←→ 翻页。"""

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("enter", "open_selected", show=False),
        Binding("left", "page_prev", show=False),
        Binding("right", "page_next", show=False),
        Binding("escape", "cancel", show=False),
    ]
    DEFAULT_CSS = """
    ResumePicker {
        width: 100%;
        height: auto;
        layout: vertical;
        padding: 1 1 0 1;
    }
    ResumePicker:focus {
        outline: none;
    }
    ResumePicker .resume-head {
        height: 1;
        color: #767676;
        padding: 0 2;
        text-style: none;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._cursor = 0

    def _picks(self) -> List[SessionPickRow]:
        return list(self.query(SessionPickRow))

    def paint_cursor(self) -> None:
        picks = self._picks()
        if not picks:
            return
        self._cursor = max(0, min(self._cursor, len(picks) - 1))
        for i, row in enumerate(picks):
            row.set_class(i == self._cursor, "selected")

    def action_cursor_up(self) -> None:
        if self._cursor <= 0:
            turn = getattr(self.app, "_resume_turn_page", None)
            if callable(turn) and getattr(self.app, "_resume_page", 0) > 0:
                turn(-1)
            return
        self._cursor -= 1
        self.paint_cursor()

    def action_cursor_down(self) -> None:
        picks = self._picks()
        if self._cursor >= len(picks) - 1:
            turn = getattr(self.app, "_resume_turn_page", None)
            if callable(turn):
                turn(1)
            return
        self._cursor += 1
        self.paint_cursor()

    def action_open_selected(self) -> None:
        picks = self._picks()
        if not picks:
            return
        picks[self._cursor].on_click()

    def action_page_prev(self) -> None:
        turn = getattr(self.app, "_resume_turn_page", None)
        if callable(turn):
            turn(-1)

    def action_page_next(self) -> None:
        turn = getattr(self.app, "_resume_turn_page", None)
        if callable(turn):
            turn(1)

    def action_cancel(self) -> None:
        cancel = getattr(self.app, "_resume_cancel", None)
        if callable(cancel):
            self.app.run_worker(cancel(), exclusive=True, group="session")


class DiffLine(Static):
    """一行补丁：行号 gutter + 高亮代码，背景铺满整行。"""

    ALLOW_SELECT = True
    DEFAULT_CSS = """
    DiffLine {
        width: 100%;
        height: 1;
        padding: 0 1 0 2;
    }
    DiffLine.add { background: #daf2dc; }
    DiffLine.del { background: #f5dade; }
    """

    def __init__(self, kind: str, ln: int, body: str, filename: str = "") -> None:
        gutter = Text(f"{ln:>5}  ", style="#767676")
        renderable = gutter + highlight_code_line(body, filename)
        super().__init__(renderable, markup=False, classes=kind, expand=True)


class DiffBlock(VerticalGroup):
    """Edit / Write 成功后的红绿补丁。"""

    DEFAULT_CSS = """
    DiffBlock {
        height: auto;
        width: 100%;
        margin: 0 0 1 0;
        padding: 0 2 0 2;
        layout: vertical;
    }
    """

    def __init__(
        self,
        rows: List[dict],
        extra: str = "",
        filename: str = "",
    ) -> None:
        super().__init__()
        self._rows = rows
        self._extra = extra
        self._filename = filename

    def compose(self):
        for row in self._rows:
            yield DiffLine(
                str(row.get("kind") or "ctx"),
                int(row.get("ln") or 0),
                str(row.get("body") or ""),
                self._filename,
            )
        if self._extra:
            yield Static(_muted(self._extra), classes="muted")


class Prose(Static):
    """一段回复：用 Rich 画 markdown，再转成 Text，才能拖选复制。"""

    ALLOW_SELECT = True
    DEFAULT_CSS = """
    Prose {
        height: auto;
        width: 1fr;
        margin: 1 2 1 2;
        background: #f5f5f5;
        color: #444444;
    }
    """

    def __init__(self, markdown: str = "") -> None:
        self._src = markdown or ""
        self._paint_width = 80
        super().__init__(_markdown_as_text(self._src, 80), shrink=True, markup=False)

    def set_markdown(self, markdown: str) -> None:
        self._src = markdown or ""
        self._repaint()

    def on_mount(self) -> None:
        self._repaint()

    def on_resize(self) -> None:
        # 滚动条出现会让宽度抖 1 列；差 1 不重画，避免滑动时整段 markdown 重渲。
        if abs(int(self.size.width) - self._paint_width) >= 2:
            self._repaint()

    def _repaint(self) -> None:
        width = int(self.content_size.width or self.size.width or 80)
        if width <= 0:
            width = 80
        self._paint_width = width
        self.update(_markdown_as_text(self._src, width))

    def get_selection(self, selection: Selection) -> Optional[Tuple[str, str]]:
        visual = self._render()
        width = max(int(self.content_size.width), 1)
        strips = visual.render_strips(
            width,
            None,
            self.visual_style,
            RenderOptions(self._get_style, self.styles, None, None),
        )
        text = "\n".join(strip.text for strip in strips)
        extracted = selection.extract(text)
        if not extracted:
            return None
        return extracted, "\n"


class PromptInput(Input):
    """粘贴：整段绝对图片路径或剪贴板位图变成 [Image #N]。chip 整块删、整块跳。"""

    BINDINGS = [
        Binding("tab", "slash_tab", show=False),
        Binding("up", "slash_up", show=False),
        Binding("down", "slash_down", show=False),
        Binding("ctrl+c,super+c", "copy", show=False),
        Binding("escape", "abort_key", show=False),
    ]
    DEFAULT_CSS = """
    PromptInput {
        color: #1d1d1f;
    }
    PromptInput > .input--value {
        color: #1d1d1f;
    }
    PromptInput > .input--cursor {
        background: #000000;
        color: #ffffff;
        text-style: none;
    }
    PromptInput > .input--placeholder {
        color: #8e8e93;
    }
    PromptInput > .input--suggestion {
        color: #8e8e93;
    }
    PromptInput.-slash-cmd {
        color: #2F64D2;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cursor_blink = False

    def _restart_blink(self) -> None:
        self._pause_blink(visible=True)

    def action_abort_key(self) -> None:
        abort = getattr(self.app, "action_abort_turn", None)
        if callable(abort):
            abort()

    def action_copy(self) -> None:
        app = self.app
        if getattr(app, "_busy", False) and not (self.selected_text or "").strip():
            abort = getattr(app, "action_abort_turn", None)
            if callable(abort):
                abort()
            return
        selected = self.selected_text
        if selected:
            self.app.copy_to_clipboard(selected)
            return
        screen_text = self.screen.get_selected_text()
        if screen_text:
            self.app.copy_to_clipboard(screen_text)
            return
        raise SkipAction()

    def action_slash_tab(self) -> None:
        complete = getattr(self.app, "complete_slash", None)
        if callable(complete) and complete():
            return

    def action_slash_up(self) -> None:
        move = getattr(self.app, "move_slash", None)
        if callable(move) and move(-1):
            return

    def action_slash_down(self) -> None:
        move = getattr(self.app, "move_slash", None)
        if callable(move) and move(1):
            return


    def _sync_draft(self) -> None:
        sync = getattr(self.app, "sync_draft_images", None)
        if callable(sync):
            sync(self.value)

    def _delete_span(self, start: int, end: int) -> None:
        self.delete(*expand_range_to_chips(self.value, start, end))
        self._sync_draft()

    def _on_paste(self, event: Paste) -> None:
        attach = getattr(self.app, "attach_paste_text", None)
        if callable(attach) and attach(event.text or ""):
            event.stop()
            return
        super()._on_paste(event)

    def action_paste(self) -> None:
        attach = getattr(self.app, "attach_paste_text", None)
        clip_text = getattr(self.app, "clipboard", "") or ""
        if callable(attach) and clip_text and attach(clip_text):
            return
        clip_img = getattr(self.app, "attach_clipboard_image", None)
        if callable(clip_img) and clip_img():
            return
        super().action_paste()

    def action_delete_left(self) -> None:
        if not self.selection.is_empty:
            self._delete_span(*self.selection)
            return
        span = chip_for_backspace(self.value, self.cursor_position)
        if span:
            self._delete_span(*span)
            return
        super().action_delete_left()
        self._sync_draft()

    def action_delete_right(self) -> None:
        if not self.selection.is_empty:
            self._delete_span(*self.selection)
            return
        span = chip_for_delete(self.value, self.cursor_position)
        if span:
            self._delete_span(*span)
            return
        super().action_delete_right()
        self._sync_draft()

    def action_delete_left_word(self) -> None:
        if not self.selection.is_empty:
            self._delete_span(*self.selection)
            return
        span = chip_for_backspace(self.value, self.cursor_position)
        if span:
            self._delete_span(*span)
            return
        super().action_delete_left_word()
        self._sync_draft()

    def action_delete_right_word(self) -> None:
        if not self.selection.is_empty:
            self._delete_span(*self.selection)
            return
        span = chip_for_delete(self.value, self.cursor_position)
        if span:
            self._delete_span(*span)
            return
        super().action_delete_right_word()
        self._sync_draft()

    def action_cursor_left(self, select: bool = False) -> None:
        if not select and self.selection.is_empty:
            span = chip_for_backspace(self.value, self.cursor_position)
            if span:
                self.cursor_position = span[0]
                return
        super().action_cursor_left(select)
        if not select:
            snapped = snap_cursor_out_of_chip(self.value, self.cursor_position)
            if snapped != self.cursor_position:
                self.cursor_position = snapped

    def action_cursor_right(self, select: bool = False) -> None:
        if not select and self.selection.is_empty:
            span = chip_for_delete(self.value, self.cursor_position)
            if span:
                self.cursor_position = span[1]
                return
        super().action_cursor_right(select)
        if not select:
            snapped = snap_cursor_out_of_chip(self.value, self.cursor_position)
            if snapped != self.cursor_position:
                self.cursor_position = snapped

    async def _on_key(self, event) -> None:
        if event.is_printable and self.selection.is_empty:
            span = chip_for_delete(self.value, self.cursor_position)
            if span and span[0] < self.cursor_position < span[1]:
                event.stop()
                self._delete_span(*span)
                self.insert(event.character or "", self.cursor_position)
                return
        await super()._on_key(event)

    async def _on_mouse_down(self, event) -> None:
        await super()._on_mouse_down(event)
        snapped = snap_cursor_out_of_chip(self.value, self.cursor_position)
        if snapped != self.cursor_position:
            self.cursor_position = snapped


def slash_row_text(name: str, hint: str, selected: bool = False, prefix: str = "/") -> Text:
    mark = "›" if selected else " "
    body = Text()
    body.append(f"{mark} ", style=_TEXT)
    typed = prefix if prefix and prefix != "/" else ""
    matched = len(typed) if typed and name.lower().startswith(typed.lower()) else 0
    if matched:
        body.append(name[:matched], style=_BLUE)
        body.append(name[matched:], style=_TEXT)
    else:
        body.append(name, style=_TEXT)
    body.append(" " * max(1, 12 - len(name)))
    body.append(hint, style=_MUTED)
    return body


class SlashRow(Static):
    DEFAULT_CSS = """
    SlashRow {
        color: auto;
    }
    """

    def __init__(
        self,
        name: str,
        hint: str,
        needs_arg: bool,
        selected: bool = False,
        prefix: str = "/",
    ) -> None:
        super().__init__(slash_row_text(name, hint, selected, prefix), markup=False)
        self.cmd_name = name
        self.hint = hint
        self.needs_arg = needs_arg
        self.set_class(selected, "selected")

    def on_click(self) -> None:
        pick = getattr(self.app, "pick_slash_row", None)
        if callable(pick):
            pick(self.cmd_name, self.needs_arg)


class SlashMenu(VerticalGroup):
    """输入 / 时贴在输入框上方的命令列表。"""

    DEFAULT_CSS = """
    SlashMenu {
        width: 100%;
        height: auto;
        max-height: 10;
        layout: vertical;
        background: #e4e4e6;
        padding: 0 0;
        display: none;
    }
    SlashMenu SlashRow {
        width: 1fr;
        height: 1;
        padding: 0 1;
    }
    SlashMenu SlashRow.selected {
        background: #d0d0d4;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="slash-menu")
        self.matches: List[Tuple[str, str, bool]] = []
        self.index = 0
        self.prefix = "/"

    @property
    def is_open(self) -> bool:
        return bool(self.display) and bool(self.matches)

    @property
    def current(self) -> Optional[Tuple[str, str, bool]]:
        if not self.matches:
            return None
        return self.matches[self.index]

    def close(self) -> None:
        self.matches = []
        self.index = 0
        self.prefix = "/"
        self.display = False
        for child in list(self.children):
            child.remove()

    def show(self, rows: List[Tuple[str, str, bool]], prefix: str = "/") -> None:
        keep = self.current[0] if self.current else None
        self.matches = rows
        self.prefix = prefix
        if not rows:
            self.close()
            return
        names = [item[0] for item in rows]
        matching_names = [name for name in names if name.lower().startswith(prefix.lower())]
        if keep in matching_names:
            self.index = names.index(keep)
        elif matching_names:
            self.index = names.index(matching_names[0])
        else:
            self.index = 0
        self.display = True
        self._rebuild()

    def move(self, delta: int) -> bool:
        if not self.is_open:
            return False
        self.index = (self.index + delta) % len(self.matches)
        self._rebuild()
        return True

    def _rebuild(self) -> None:
        for child in list(self.children):
            child.remove()
        for i, (name, hint, needs_arg) in enumerate(self.matches):
            self.mount(
                SlashRow(
                    name,
                    hint,
                    needs_arg,
                    selected=(i == self.index),
                    prefix=self.prefix,
                )
            )


class Timeline(VerticalScroll):
    """时间线：滚动立刻跳，不要默认的惯性动画。"""

    def action_scroll_up(self) -> None:
        self.scroll_up(animate=False, immediate=True)

    def action_scroll_down(self) -> None:
        self.scroll_down(animate=False, immediate=True)

    def action_page_up(self) -> None:
        self.scroll_page_up(animate=False, immediate=True)

    def action_page_down(self) -> None:
        self.scroll_page_down(animate=False, immediate=True)

    def action_scroll_home(self) -> None:
        self.scroll_home(animate=False, immediate=True)

    def action_scroll_end(self) -> None:
        self.scroll_end(animate=False, immediate=True)


def _shrink_ascii(text: str, fy: int = 2, fx: int = 2) -> str:
    """把点阵按块收成更小的一块，启动卡片用。"""
    lines = [line.rstrip() for line in text.splitlines()]
    if not lines:
        return text
    width = max(len(line) for line in lines)
    padded = [line.ljust(width) for line in lines]
    rank = {ch: i for i, ch in enumerate(" `.'-:,;^=+/<>*|#%&$@")}
    out: List[str] = []
    for y in range(0, len(padded), fy):
        row: List[str] = []
        for x in range(0, width, fx):
            block = ""
            for dy in range(fy):
                if y + dy < len(padded):
                    block += padded[y + dy][x : x + fx]
            best, best_d = " ", -1
            for ch in block:
                dens = rank.get(ch, 8)
                if dens > best_d:
                    best, best_d = ch, dens
            row.append(best)
        out.append("".join(row).rstrip())
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    indent = min((len(line) - len(line.lstrip()) for line in out if line.strip()), default=0)
    dotted = []
    for line in out:
        dotted.append("".join("." if ch != " " else " " for ch in line[indent:]))
    return "\n".join(dotted)


ENDURANCE_SRC = Path(__file__).with_name("endurance.txt").read_text(encoding="utf-8")
ENDURANCE_ART = _shrink_ascii(ENDURANCE_SRC)


def _splash_copy() -> Text:
    text = Text()
    text.append("Permanent", style="bold")
    text.append("  local coding agent\n\n", style="#8e8e93")
    for name, key in (
        ("New session", "/new"),
        ("Resume", "/resume"),
        ("Help", "/help"),
    ):
        text.append(f"{name:<22}", style="#262626")
        text.append(f"{key}\n", style="#8e8e93")
    return text


class SplashCard(Horizontal):
    """空会话时的居中卡片：永恒号 + Permanent。"""

    DEFAULT_CSS = """
    SplashCard {
        width: 88;
        max-width: 100%;
        height: auto;
        padding: 2 3;
        border: round #d5d5d8;
        background: #f5f5f5;
        layout: horizontal;
        align: left middle;
    }
    SplashCard #ship {
        width: auto;
        height: auto;
        color: #8e8e93;
        padding: 0 3 0 1;
    }
    SplashCard #blurb {
        width: 1fr;
        height: auto;
        padding: 1 1 1 2;
        color: #262626;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(ENDURANCE_ART, id="ship", markup=False)
        yield Static(_splash_copy(), id="blurb")


class SparkTui(App):
    TITLE = "Permanent"
    ALLOW_SELECT = True
    CSS = """
    Screen {
        layout: vertical;
        background: #f5f5f5;
        color: #444444;
    }
    #chrome {
        height: 1;
        margin: 1 0;
        padding: 0 1 0 3;
        color: #767676;
        background: #f5f5f5;
    }
    #timeline {
        height: 1fr;
        background: #f5f5f5;
        overflow-x: hidden;
        overflow-y: scroll;
    }
    #timeline.-splash {
        align: center middle;
        overflow-y: hidden;
    }
    #timeline > .turn {
        height: auto;
        width: 1fr;
        layout: vertical;
        margin: 0;
        padding: 0 0 0 2;
    }
    #timeline TimelineRow {
        height: 1;
        margin: 0;
        padding: 0 2;
    }
    #timeline RunningToolRow {
        height: 1;
        width: 100%;
        padding: 0 2;
        background: #f5f5f5;
    }
    #timeline SessionPickRow {
        height: 1;
        padding: 0 2;
    }
    #timeline SessionPickRow.selected {
        background: #d8d8dc;
    }
    #timeline ResumePageRow {
        height: 1;
        padding: 0 2;
    }
    #timeline TimelineRow.muted {
        height: auto;
    }
    #timeline TimelineRow.finish {
        margin-bottom: 1;
    }
    #timeline ThoughtBody {
        height: auto;
        margin: 0 2 1 2;
    }
    #timeline Prose {
        height: auto;
        margin: 1 2;
        padding: 0;
        background: #f5f5f5;
    }
    #timeline DiffBlock {
        width: 100%;
        height: auto;
    }
    #timeline DiffLine {
        width: 1fr;
        height: 1;
        padding: 0 1 0 2;
    }
    #timeline DiffLine.add { background: #daf2dc; }
    #timeline DiffLine.del { background: #f5dade; }
    #queue {
        height: auto;
        width: 100%;
        background: #f5f5f5;
        display: none;
    }
    #queue.-open {
        display: block;
    }
    #prompt-dock {
        dock: bottom;
        height: auto;
        padding: 0 2 1 2;
        background: #f5f5f5;
    }
    #prompt-wrap {
        height: 3;
        width: 100%;
        margin: 0;
        padding: 0 1;
        background: #f5f5f5;
        border: round #c7c7cc;
        align: left middle;
    }
    #prompt-wrap:focus-within {
        border: round #8e8e93;
    }
    #prompt-mark {
        width: 2;
        height: 1;
        color: #8e8e93;
        content-align: left middle;
    }
    #prompt {
        width: 1fr;
        height: 1;
        color: #1d1d1f;
        background: #f5f5f5;
        border: none;
        padding: 0 1 0 0;
    }
    #prompt:focus {
        border: none;
        background-tint: 0%;
        color: #1d1d1f;
    }
    #prompt > .input--value {
        color: #1d1d1f;
    }
    #prompt > .input--cursor {
        background: #000000;
        color: #ffffff;
        text-style: none;
    }
    #prompt > .input--placeholder {
        color: #8e8e93;
    }
    #prompt.-slash-cmd {
        color: #2F64D2;
    }
    """
    BINDINGS = [
        Binding("ctrl+c", "copy_selection", "Copy", show=False),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "abort_turn", "Abort"),
        Binding("pageup", "page_up", "Scroll up", show=False),
        Binding("pagedown", "page_down", "Scroll down", show=False),
        Binding("ctrl+up", "page_up", "Scroll up", show=False),
        Binding("ctrl+down", "page_down", "Scroll down", show=False),
    ]

    def __init__(self, session_id: str, workspace: str) -> None:
        super().__init__()
        self.scroll_sensitivity_y = 4.0
        self.session_id = session_id
        self.workspace = workspace
        from agent_loop.plugins import PluginHost

        self.plugins = PluginHost()
        self.loop = ReactAgentLoop(CliDeps(), None, self.plugins)
        self._busy = False
        self._unsub = None
        self._pending_user = None
        self._stream_buf = ""
        self._turn: Optional[VerticalGroup] = None
        self._md: Optional[Prose] = None
        self._thought: Optional[TimelineRow] = None
        self._thought_body: Optional[ThoughtBody] = None
        self._thought_buf = ""
        self._last_thought_paint = 0.0
        self._think_t0: Optional[float] = None
        self._had_reasoning = False
        self._busy_t0: Optional[float] = None
        self._aborting = False
        self._context_used: Optional[int] = None
        self._context_limit: Optional[int] = None
        self._compactions: List[Dict] = []
        self._tools_shown = 0
        self._ellipsis = False
        self._running_tool: Optional[RunningToolRow] = None
        self._waiting_notice: Optional[WaitingNotice] = None
        self._follow = True
        self._last_stream_paint = 0.0
        self._resume_items: List[Dict] = []
        self._resume_page = 0
        self._resume_from_id = ""
        self._draft_images: List[Dict] = []
        self._attaching_drop = False
        self._queue_rows: Dict[str, QueuedMessage] = {}
        self._banner_shown: List[str] = []
        self._stdio_log_handlers: List = []

    def compose(self) -> ComposeResult:
        yield Static("", id="chrome")
        yield Timeline(id="timeline")
        yield Vertical(id="queue")
        yield SlashMenu()
        with Vertical(id="prompt-dock"):
            with Horizontal(id="prompt-wrap"):
                yield Static("›", id="prompt-mark")
                yield PromptInput(placeholder="Message or /help", id="prompt", compact=True)

    def on_click(self, event) -> None:
        widget = event.widget
        while widget is not None and widget is not self:
            if widget.id == "prompt":
                return
            if widget.id in {"prompt-wrap", "prompt-dock"}:
                self.query_one("#prompt", Input).focus()
                return
            widget = widget.parent

    def _chat_dir(self):
        return session_log_path_for(self.workspace, self.session_id).parent

    def _bind_session_logs(self) -> None:
        from agent_loop.file_logging import configure_file_logging

        chat = self._chat_dir()
        configure_file_logging(chat)
        self.plugins.set_session_dir(chat)

    def on_mount(self) -> None:
        events.print_to_stdout = False
        self._stdio_log_handlers = _quiet_stdio_logging()
        self.console.push_theme(_PROSE_THEME)
        self._unsub = events.subscribe(self._on_event)
        path = session_log_path_for(self.workspace, self.session_id)
        self._bind_session_logs()
        self._refresh_chrome()
        self.set_interval(0.1, self._tick_think)
        self._replay_log(path)
        self._maybe_show_splash()
        self.query_one("#prompt", Input).focus()
        self.run_worker(self.plugins.ensure_started, exclusive=True, group="plugins")
        self.run_worker(
            self._peek_update_worker,
            exclusive=True,
            group="update",
            thread=True,
        )

    def _peek_update_worker(self) -> None:
        from agent_loop.update import peek_update

        notice = peek_update()
        if notice:
            self.call_from_thread(self._show_update_notice, notice)

    def _show_update_notice(self, notice: str) -> None:
        self._timeline().mount(TimelineRow(_mark(notice), classes="muted"))

    def on_unmount(self) -> None:
        events.print_to_stdout = True
        _restore_stdio_logging(self._stdio_log_handlers)
        self._stdio_log_handlers = []
        try:
            self.console.pop_theme()
        except Exception:
            pass
        if self._unsub:
            self._unsub()
            self._unsub = None
        self.plugins.stop_sync()

    def _timeline(self) -> Timeline:
        return self.query_one("#timeline", VerticalScroll)

    def _timeline_is_empty(self) -> bool:
        tl = self._timeline()
        return not list(tl.query(".turn")) and not list(tl.query(ResumePicker))

    def _show_splash(self) -> None:
        tl = self._timeline()
        if list(tl.query(SplashCard)):
            return
        tl.add_class("-splash")
        tl.mount(SplashCard())

    def _hide_splash(self) -> None:
        tl = self._timeline()
        tl.remove_class("-splash")
        for card in list(tl.query(SplashCard)):
            card.remove()

    def _maybe_show_splash(self) -> None:
        if self._timeline_is_empty():
            self._show_splash()

    def _start_turn(self, text: str) -> VerticalGroup:
        self._hide_splash()
        self._end_running_tool()
        self._end_think()
        turn = VerticalGroup(classes="turn")
        self._timeline().mount(turn)
        turn.mount(UserBanner(f"›  {text}"))
        self._turn = turn
        self._md = None
        self._stream_buf = ""
        self._thought_body = None
        self._thought_buf = ""
        self._last_thought_paint = 0.0
        self._tools_shown = 0
        self._ellipsis = False
        self._context_used = None
        self._context_limit = None
        self._compactions = []
        self._follow = True
        self._scroll_follow()
        return turn

    def _tick_think(self) -> None:
        if self._aborting:
            return
        if self._thought is not None and self._think_t0 is not None:
            self._thought.update(_thinking_line(time.monotonic() - self._think_t0))

    def _begin_think(self) -> None:
        if self._turn is None:
            self._start_turn("")
        if self._thought is not None:
            self._had_reasoning = True
            return
        assert self._turn is not None
        self._had_reasoning = True
        self._think_t0 = time.monotonic()
        self._thought = TimelineRow(_thinking_line(0.0), classes="thought")
        self._turn.mount(self._thought)
        self._tick_think()
        self._scroll_follow()

    def _end_think(self) -> None:
        dt = None
        if self._think_t0 is not None:
            dt = time.monotonic() - self._think_t0
        row = self._thought
        had = self._had_reasoning
        buf = self._thought_buf
        body = self._thought_body
        self._thought = None
        self._think_t0 = None
        self._had_reasoning = False
        self._thought_buf = ""
        self._thought_body = None
        # 工具之间一闪而过、又没有正文的 Thought 0.2s，留着只是添乱。
        keep = had and dt is not None and not (dt < 1.0 and not (buf or "").strip())
        if row is not None:
            if keep:
                row.update(_thought_line(dt))
            else:
                row.remove()
                if body is not None:
                    body.remove()
                    body = None
        elif keep and self._turn is not None:
            self._turn.mount(TimelineRow(_thought_line(dt), classes="thought"))
            self._scroll_follow()
        # 思考原文往往折成两三行，时间线上只留 Thought for Xs。
        if body is not None:
            body.remove()

    def _end_running_tool(self) -> None:
        row = self._running_tool
        self._running_tool = None
        if row is None or not row.is_attached:
            return
        row.freeze()

    def _begin_running_tool(self, name: str, args: Optional[Dict] = None) -> None:
        self._end_running_tool()
        if self._turn is None:
            return
        if self._tools_shown >= _STEP_TOOLS_MAX:
            if not self._ellipsis:
                self._turn.mount(TimelineRow(_mark(f"[{_MUTED}]…[/]"), classes="tool"))
                self._ellipsis = True
            return
        self._tools_shown += 1
        row = RunningToolRow(name, args)
        self._running_tool = row
        self._turn.mount(row)

    def _add_tool_row(self, name: str, args: Optional[Dict] = None) -> None:
        if self._turn is None:
            return
        if self._tools_shown >= _STEP_TOOLS_MAX:
            if not self._ellipsis:
                self._turn.mount(TimelineRow(_mark(f"[{_MUTED}]…[/]"), classes="tool"))
                self._ellipsis = True
            return
        self._tools_shown += 1
        self._turn.mount(
            TimelineRow(
                _mark(_bold_verb(format_grok_tool(name, args))),
                classes="tool",
            )
        )

    def _close_prose(self) -> None:
        self._md = None
        self._stream_buf = ""

    def _ensure_md(self) -> Prose:
        self._end_think()
        if self._md is None:
            if self._turn is None:
                self._start_turn("")
            assert self._turn is not None
            self._md = Prose("")
            self._turn.mount(self._md)
        return self._md

    def _replay_log(self, log_path) -> None:
        log = SessionLog(log_path)
        for row in log.read_all():
            kind = row.get("kind")
            typ = row.get("type")
            if kind == "entry" and typ == "user":
                self._start_turn(str(row.get("content") or ""))
            elif kind == "entry" and typ == "assistant":
                text = str(row.get("content") or "").rstrip()
                if text and self._turn is not None:
                    self._turn.mount(Prose(text))
                    self._md = None
                    self._tools_shown = 0
                    self._ellipsis = False
            elif kind == "record" and typ == "tool_started":
                name = str(row.get("tool_name") or "")
                args = row.get("effective_args") or {}
                if self._turn is None:
                    continue
                self._end_think()
                self._add_tool_row(name, args)
        self._follow = True
        self._scroll_follow()

    def _scroll_follow(self) -> None:
        if not self._follow:
            return
        self._timeline().scroll_end(animate=False)

    def _on_event(self, event: dict) -> None:
        self.call_later(self._handle_event, event)

    def _handle_event(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "user":
            text = str(event.get("text") or "")
            if text == self._pending_user:
                self._pending_user = None
                return
            if text in self._banner_shown:
                self._banner_shown.remove(text)
                return
            self._remove_queue_by_text(text)
            self._start_turn(text)
        elif kind == "assistant_delta":
            channel = str(event.get("channel") or "content")
            piece = str(event.get("text") or "")
            if channel == "reasoning":
                if piece:
                    self._thought_buf += piece
                    self._begin_think()
                return
            self._end_think()
            if channel == "tool" or not piece:
                return
            self._stream_buf += piece
            now = time.monotonic()
            if now - self._last_stream_paint >= 0.05:
                self._last_stream_paint = now
                self._ensure_md().set_markdown(self._stream_buf)
                self._scroll_follow()
        elif kind == "assistant":
            self._end_think()
            full = str(event.get("text") or "").rstrip()
            if full:
                self._ensure_md().set_markdown(full)
            self._close_prose()
            self._tools_shown = 0
            self._ellipsis = False
            self._scroll_follow()
        elif kind == "tools":
            self._end_think()
            if self._md is not None:
                self._close_prose()
            self._tools_shown = 0
            self._ellipsis = False
        elif kind == "tool_start":
            name = str(event.get("name") or "tool")
            args = event.get("args") or {}
            if self._turn is None:
                return
            self._end_think()
            if self._md is not None:
                self._close_prose()
            self._begin_running_tool(name, args)
            self._scroll_follow()
        elif kind == "tool_result":
            self._end_running_tool()
            self._stop_waiting_notice()
        elif kind in ("edit_diff", "write_diff"):
            if self._turn is None:
                return
            rows = event.get("rows") or []
            extra = str(event.get("extra") or "")
            path = str(event.get("path") or "")
            if rows or extra:
                self._turn.mount(DiffBlock(rows, extra=extra, filename=path))
                self._scroll_follow()
        elif kind == "context":
            self._context_used = int(event.get("used") or 0)
            self._context_limit = int(event.get("limit") or 0)
        elif kind == "compaction":
            self._compactions.append(
                {
                    "level": str(event.get("level") or "compact"),
                    "before": float(event.get("before") or 0),
                    "after": float(event.get("after") or 0),
                }
            )
        elif kind == "notice":
            self._end_think()
            text = str(event.get("text") or "").strip()
            if not text:
                return
            if self._turn is None:
                self._start_turn("")
            self._stop_waiting_notice()
            row = WaitingNotice(text)
            self._waiting_notice = row
            self._turn.mount(row)
            self._scroll_follow()
        elif kind == "error":
            self._stop_waiting_notice()
            self._end_think()
            if self._turn is not None:
                self._turn.mount(
                    TimelineRow(_mark(f"error  {event.get('text') or ''}"), classes="muted")
                )

    def _stop_waiting_notice(self) -> None:
        row = self._waiting_notice
        self._waiting_notice = None
        if row is not None:
            row.freeze()

    def _set_status(self, text: str) -> None:
        return

    def _session_dir(self) -> Path:
        return session_log_path_for(self.workspace, self.session_id).parent

    def sync_draft_images(self, text: str) -> None:
        used = set(placeholders_in_text(text))
        self._draft_images = [
            item for item in self._draft_images if int(item.get("n") or 0) in used
        ]

    def _insert_image_chip(self, dest: Path, mime: str, *, replace_all: bool = False) -> None:
        prompt = self.query_one("#prompt", Input)
        n = next_image_number(prompt.value, self._draft_images)
        self._draft_images.append({"n": n, "path": str(dest), "mime": mime})
        start, end = (0, len(prompt.value)) if replace_all else prompt.selection
        before = prompt.value[:start]
        after = prompt.value[end:]
        token = f"[Image #{n}]"
        if before and not before[-1].isspace():
            token = " " + token
        if after and not after[0].isspace():
            token = token + " "
        prompt.replace(token, start, end)

    def on_input_changed(self, event: Input.Changed) -> None:
        """终端拖文件常常不是 Paste 事件，而是把带引号的路径打进输入框。"""
        if event.input.id != "prompt" or self._attaching_drop:
            return
        text = event.value or ""
        dropped = try_read_dropped_paths(text)
        if dropped and any(kind == "image" for kind, _ in dropped):
            self._attaching_drop = True
            try:
                self.attach_paste_text(text, replace_all=True)
            finally:
                self._attaching_drop = False
            self._refresh_slash_menu(event.input.value or "")
            return
        self.sync_draft_images(text)
        self._refresh_slash_menu(text)

    def attach_paste_text(self, text: str, *, replace_all: bool = False) -> bool:
        dropped = try_read_dropped_paths(text)
        if not dropped:
            return False
        attached = False
        session_dir = self._session_dir()
        for kind, path in dropped:
            if kind != "image":
                prompt = self.query_one("#prompt", Input)
                start, end = (0, len(prompt.value)) if replace_all else prompt.selection
                prompt.replace(str(path) + " ", start, end)
                attached = True
                replace_all = False
                continue
            try:
                dest, mime = persist_image_file(path, session_dir)
            except (OSError, ValueError):
                continue
            self._insert_image_chip(dest, mime, replace_all=replace_all)
            attached = True
            replace_all = False
        return attached

    def attach_clipboard_image(self) -> bool:
        got = read_clipboard_image_bytes()
        if not got:
            return False
        data, mime = got
        try:
            dest = persist_image_bytes(data, mime, self._session_dir())
        except ValueError:
            return False
        self._insert_image_chip(dest, mime)
        return True

    def _take_submit_media(self, text: str) -> List[Dict]:
        used = set(placeholders_in_text(text))
        media: List[Dict] = []
        keep: List[Dict] = []
        for item in self._draft_images:
            if int(item.get("n") or 0) in used:
                media.append(
                    {
                        "kind": "image",
                        "mime": item.get("mime") or "image/png",
                        "path": item.get("path") or "",
                    }
                )
            else:
                keep.append(item)
        self._draft_images = keep
        return media

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        menu = self._slash_menu()
        typed = event.value or ""
        prefix = slash_prefix(typed)
        if (
            menu.is_open
            and menu.current
            and prefix
            and menu.current[0].lower().startswith(prefix.lower())
        ):
            name, _, needs_arg = menu.current
            if needs_arg:
                event.input.value = name + " "
                event.input.cursor_position = len(event.input.value)
                menu.close()
                return
            text = name
        else:
            text = typed.strip()
        event.input.value = ""
        menu.close()
        if not text:
            return
        cmd = parse_slash(text)
        if cmd:
            await self._run_command(cmd, text)
            return
        if text.startswith("/"):
            self._hide_splash()
            self._timeline().mount(TimelineRow(_mark("unknown command. type /help"), classes="muted"))
            return
        media = self._take_submit_media(text)
        if self._busy:
            self._enqueue(text, media)
            return
        self._busy = True
        self._aborting = False
        self._busy_t0 = time.monotonic()
        self._pending_user = text
        self._start_turn(text)
        self.run_worker(self._run_turn(text, media), exclusive=True, group="turn")

    def _queue_box(self) -> Vertical:
        return self.query_one("#queue", Vertical)

    def _sync_queue_box(self) -> None:
        box = self._queue_box()
        box.set_class(bool(self._queue_rows), "-open")

    def _renumber_queue(self) -> None:
        for index, row in enumerate(self._queue_rows.values(), start=1):
            row.set_index(index)

    def _drop_queue_row(self, qid: str) -> None:
        row = self._queue_rows.pop(qid, None)
        if row is not None and row.is_attached:
            row.remove()
        self._renumber_queue()
        self._sync_queue_box()

    def _remove_queue_by_text(self, text: str) -> None:
        for qid, row in list(self._queue_rows.items()):
            if row._text == text:
                self._drop_queue_row(qid)
                return

    def _enqueue(self, text: str, media: Optional[List[Dict]] = None) -> None:
        qid = self.loop.inbox.push_follow_up(text, media)
        if not qid:
            return
        row = QueuedMessage(qid, text, len(self._queue_rows) + 1, media)
        self._queue_rows[qid] = row
        self._queue_box().mount(row)
        self._sync_queue_box()
        self._scroll_follow()

    def send_queued_now(self, qid: str) -> None:
        row = self._queue_rows.get(qid)
        if row is None:
            return
        taken = self.loop.inbox.take_follow_up(qid)
        if taken is None:
            return
        text, media = taken
        self._drop_queue_row(qid)
        if self._busy and self._turn is not None:
            self.loop.inbox.push_steer(text, media)
            self._turn.mount(UserBanner(f"›  {text}"))
            self._banner_shown.append(text)
            self._scroll_follow()
            return
        self._busy = True
        self._aborting = False
        self._busy_t0 = time.monotonic()
        self._pending_user = text
        self._start_turn(text)
        self.run_worker(self._run_turn(text, media), exclusive=True, group="turn")

    def edit_queued(self, qid: str) -> None:
        row = self._queue_rows.get(qid)
        if row is None:
            return
        if self.loop.inbox.take_follow_up(qid) is None:
            return
        text = row._text
        media = list(row._media)
        self._drop_queue_row(qid)
        prompt = self.query_one("#prompt", Input)
        prompt.value = text
        prompt.cursor_position = len(text)
        self._draft_images = []
        numbers = placeholders_in_text(text)
        for number, item in zip(numbers, media):
            self._draft_images.append(
                {
                    "n": number,
                    "path": item.get("path") or "",
                    "mime": item.get("mime") or "image/png",
                }
            )
        prompt.focus()

    def cancel_queued(self, qid: str) -> None:
        self.loop.inbox.cancel_follow_up(qid)
        self._drop_queue_row(qid)

    async def _run_command(self, cmd: str, raw: str = "") -> None:
        arg = raw.split(None, 1)[1].strip() if len(raw.split(None, 1)) > 1 else ""
        if cmd == "exit":
            self.action_abort_turn()
            self.exit()
            return
        if cmd == "help":
            self._hide_splash()
            self._timeline().mount(Prose(HELP))
            return
        if cmd == "copy":
            self._copy_latest_reply()
            return
        if cmd == "session":
            self._hide_splash()
            path = session_log_path_for(self.workspace, self.session_id)
            self._timeline().mount(TimelineRow(_mark(str(self.session_id)), classes="muted"))
            self._timeline().mount(TimelineRow(_mark(str(path)), classes="muted"))
            return
        if cmd == "new":
            sid = datetime.now().strftime("cli-%Y%m%d-%H%M%S")
            await self._switch_session(sid, replay=False)
            return
        if cmd == "resume":
            await self._resume_session(arg)
            return
        if cmd == "rename":
            self._rename_session(arg)
            return

    def _refresh_chrome(self) -> None:
        ensure_auto_title(self.workspace, self.session_id)
        self.query_one("#chrome", Static).update(_chrome_label(self.workspace))

    def _rename_session(self, arg: str) -> None:
        if not arg:
            self._hide_splash()
            self._timeline().mount(
                TimelineRow(_mark("usage: /rename <title>  or  /rename --auto"), classes="muted")
            )
            return
        if arg.strip() == "--auto":
            meta = reset_auto_title(self.workspace, self.session_id)
        else:
            meta = set_manual_title(self.workspace, self.session_id, arg)
        self._refresh_chrome()
        shown = meta.get("title") or self.session_id
        self._hide_splash()
        self._timeline().mount(TimelineRow(_mark(f"title  {shown}"), classes="muted"))
        self._scroll_follow()

    async def _resume_session(self, arg: str) -> None:
        sessions = list_sessions(self.workspace)
        if not sessions:
            self._hide_splash()
            self._timeline().mount(TimelineRow(_mark("no sessions in this project"), classes="muted"))
            return
        if not arg:
            self._resume_from_id = self.session_id
            self._resume_items = sessions
            self._resume_page = 0
            await self._render_resume_page()
            return
        chosen = None
        if arg.isdigit():
            idx = int(arg)
            if 1 <= idx <= len(sessions):
                chosen = str(sessions[idx - 1]["id"])
        if chosen is None:
            for item in sessions:
                if str(item["id"]) == arg:
                    chosen = arg
                    break
        if chosen is None:
            matches = [str(item["id"]) for item in sessions if str(item["id"]).startswith(arg)]
            if len(matches) == 1:
                chosen = matches[0]
        if chosen is None:
            self._hide_splash()
            self._timeline().mount(
                TimelineRow(_mark(f"no session matches {arg}. type /resume"), classes="muted")
            )
            return
        await self._switch_session(chosen, replay=True)

    async def _resume_cancel(self) -> None:
        sid = self._resume_from_id or self.session_id
        log = session_log_path_for(self.workspace, sid)
        replay = log.is_file() and log.stat().st_size > 0
        await self._switch_session(sid, replay=replay)

    def _resume_turn_page(self, delta: int) -> None:
        if not self._resume_items:
            return
        pages = max(1, (len(self._resume_items) + _RESUME_PAGE - 1) // _RESUME_PAGE)
        self._resume_page = max(0, min(pages - 1, self._resume_page + delta))
        self.run_worker(self._render_resume_page(), exclusive=True, group="session")

    async def _render_resume_page(self) -> None:
        items = self._resume_items
        total = len(items)
        pages = max(1, (total + _RESUME_PAGE - 1) // _RESUME_PAGE)
        page = max(0, min(pages - 1, self._resume_page))
        self._resume_page = page
        start = page * _RESUME_PAGE
        chunk = items[start : start + _RESUME_PAGE]
        self._hide_splash()
        timeline = self._timeline()
        await timeline.remove_children()
        name = Path(self.workspace).name or "sessions"
        width = max(int(timeline.size.width) or 40, 24)
        rule = "─" * max(4, width - len(name) - 3)
        head = f"{name} {rule}"
        picker = ResumePicker()
        timeline.mount(picker)
        picker.mount(Static(head, classes="resume-head"))
        for item in chunk:
            title = str(item.get("title") or item.get("preview") or item["id"])
            picker.mount(SessionPickRow(str(item["id"]), f"›  {title}"))
        if pages > 1:
            if page > 0:
                picker.mount(ResumePageRow(-1, "←  previous"))
            if page < pages - 1:
                picker.mount(ResumePageRow(1, "next  →"))
        picker.paint_cursor()
        picker.focus()
        self._follow = True
        self._scroll_follow()

    async def _switch_session(self, session_id: str, *, replay: bool) -> None:
        abort = getattr(self.loop, "_abort", None)
        if abort is not None:
            abort.abort()
        try:
            self.workers.cancel_group("turn")
        except Exception:
            pass
        self.session_id = session_id
        self.loop = ReactAgentLoop(CliDeps(), None, self.plugins)
        self._bind_session_logs()
        self._busy = False
        self._aborting = False
        self._pending_user = None
        self._turn = None
        self._md = None
        self._stream_buf = ""
        self._thought = None
        self._thought_body = None
        self._thought_buf = ""
        self._think_t0 = None
        self._had_reasoning = False
        self._busy_t0 = None
        self._context_used = None
        self._context_limit = None
        self._compactions = []
        self._tools_shown = 0
        self._ellipsis = False
        self._end_running_tool()
        self._resume_items = []
        self._resume_page = 0
        self._resume_from_id = ""
        self._draft_images = []
        self._attaching_drop = False
        self._queue_rows = {}
        self._banner_shown = []
        try:
            queue = self.query_one("#queue", Vertical)
            await queue.remove_children()
            queue.remove_class("-open")
        except Exception:
            pass
        timeline = self._timeline()
        await timeline.remove_children()
        if replay:
            ensure_auto_title(self.workspace, self.session_id)
            self._replay_log(session_log_path_for(self.workspace, self.session_id))
            self._refresh_chrome()
            self._set_status(_IDLE)
            self._maybe_show_splash()
            self._timeline().focus()
            return
        self._refresh_chrome()
        self._set_status(_IDLE)
        self._show_splash()
        self.query_one("#prompt", Input).focus()

    async def _run_turn(self, query: str, media: Optional[List[Dict]] = None) -> None:
        uad = {
            "sessionId": self.session_id,
            "workspace": self.workspace,
            "install_sigint": False,
        }
        if media:
            uad["media"] = media
        try:
            await self.loop._run_loop(query, uad)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            events.emit("error", text=str(exc) or type(exc).__name__)
        finally:
            self._finish_turn()
            self._refresh_chrome()
            if self._follow:
                self.query_one("#prompt", Input).focus()

    def _model_name(self) -> str:
        llm = getattr(self.loop, "_llm", None)
        name = getattr(llm, "model", None)
        if name:
            return str(name)
        from agent_loop.llm.deepseek import MODEL

        return MODEL

    def _context_text(self) -> str:
        if not self._context_limit:
            return ""
        used = self._context_used or 0
        return f"{format_token_short(used)} / {format_token_short(self._context_limit)}"

    def _finish_line(self, dt: float) -> str:
        parts = [f"Worked for {format_elapsed(dt)}"]
        model = self._model_name()
        if model:
            parts.append(model)
        ctx = self._context_text()
        if ctx:
            parts.append(ctx)
        for item in self._compactions:
            level = item.get("level") or "compact"
            before = float(item.get("before") or 0) * 100
            after = float(item.get("after") or 0) * 100
            parts.append(f"compacted {level} {before:.0f}% -> {after:.0f}%")
        return " | ".join(parts)

    def _finish_turn(self) -> None:
        dt = None
        if self._busy_t0 is not None:
            dt = time.monotonic() - self._busy_t0
        self._stop_waiting_notice()
        self._end_running_tool()
        self._end_think()
        self._busy = False
        self._busy_t0 = None
        self._aborting = False
        if dt is None:
            self._set_status(_IDLE)
            return
        text = self._finish_line(dt)
        turn = self._turn
        if turn is not None and turn.is_attached and self.is_running:
            try:
                turn.mount(TimelineRow(_muted(text), classes="muted finish"))
                self._scroll_follow()
            except Exception:
                logging.debug("skip finish line; timeline already gone", exc_info=True)
        self._set_status(_IDLE)

    def _latest_reply(self) -> str:
        found = ""
        for prose in self.query(Prose):
            src = prose._src or ""
            if not src.strip() or src.strip() == HELP.strip():
                continue
            found = src
        return found

    def _copy_latest_reply(self) -> None:
        self._hide_splash()
        text = self._latest_reply()
        if not text.strip():
            self._timeline().mount(
                TimelineRow(_mark("nothing to copy"), classes="muted")
            )
            return
        path = spark_home() / "last-copy.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.copy_to_clipboard(text)
        self._timeline().mount(
            TimelineRow(_muted(_copy_notice(text, path)), classes="muted")
        )

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)
        if sys.platform != "darwin" or not text:
            return
        try:
            subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                check=False,
                timeout=2,
            )
        except Exception:
            logging.debug("[tui] pbcopy failed", exc_info=True)

    def on_text_selected(self, event: TextSelected) -> None:
        text = self.screen.get_selected_text()
        if text and text.strip():
            self.copy_to_clipboard(text)

    def action_copy_selection(self) -> None:
        text = self.screen.get_selected_text()
        if not text:
            raise SkipAction()
        self.copy_to_clipboard(text)

    def _slash_menu(self) -> SlashMenu:
        return self.query_one("#slash-menu", SlashMenu)

    def _refresh_slash_menu(self, text: str) -> None:
        prefix = slash_prefix(text)
        menu = self._slash_menu()
        if prefix is None:
            menu.close()
            self.query_one("#prompt", Input).set_class(False, "-slash-cmd")
            self._sync_slash_suggestion()
            return
        hits = matching_slash(prefix)
        if not hits:
            menu.close()
            self.query_one("#prompt", Input).set_class(True, "-slash-cmd")
            self._sync_slash_suggestion()
            return
        menu.show(hits, prefix)
        prompt = self.query_one("#prompt", Input)
        prompt.set_class(True, "-slash-cmd")
        self._sync_slash_suggestion()

    def _sync_slash_suggestion(self) -> None:
        prompt = self.query_one("#prompt", Input)
        menu = self._slash_menu()
        value = prompt.value or ""
        suggestion = ""
        if menu.is_open and menu.current:
            name = menu.current[0]
            if name.lower().startswith(value.lower()):
                suggestion = value + name[len(value) :]
        prompt._suggestion = suggestion

    def complete_slash(self) -> bool:
        menu = self._slash_menu()
        if not menu.is_open:
            return False
        prompt = self.query_one("#prompt", Input)
        prefix = slash_prefix(prompt.value or "") or ""
        current = menu.current
        if current and current[0].lower().startswith(prefix.lower()):
            name, _, needs_arg = current
        else:
            hits = matching_slash(prefix)
            if not hits:
                return False
            name, _, needs_arg = hits[0]
        filled = name + (" " if needs_arg else "")
        prompt.value = filled
        prompt.cursor_position = len(filled)
        if needs_arg:
            menu.close()
        else:
            self._refresh_slash_menu(filled)
        return True

    def move_slash(self, delta: int) -> bool:
        moved = self._slash_menu().move(delta)
        if moved:
            self._sync_slash_suggestion()
        return moved

    def pick_slash_row(self, name: str, needs_arg: bool) -> None:
        prompt = self.query_one("#prompt", Input)
        if needs_arg:
            prompt.value = name + " "
            prompt.cursor_position = len(prompt.value)
            self._slash_menu().close()
            prompt.focus()
            return
        prompt.value = ""
        self._slash_menu().close()
        cmd = parse_slash(name)
        if cmd:
            self.run_worker(self._run_command(cmd, name), exclusive=True, group="session")

    def action_abort_turn(self) -> None:
        menu = self._slash_menu()
        if menu.is_open:
            menu.close()
            self._sync_slash_suggestion()
            return
        if list(self.query(ResumePicker)):
            self.run_worker(self._resume_cancel(), exclusive=True, group="session")
            return
        abort = getattr(self.loop, "_abort", None)
        if abort is not None:
            abort.abort()
        plugins = getattr(self, "plugins", None)
        cancel = getattr(plugins, "cancel", None)
        if callable(cancel):
            cancel()
        self._aborting = True
        self._end_think()
        try:
            self.workers.cancel_group("turn")
        except Exception:
            pass
        self._set_status("aborting…")

    def action_page_up(self) -> None:
        self._follow = False
        self._timeline().scroll_page_up(animate=False, immediate=True)

    def action_page_down(self) -> None:
        tl = self._timeline()
        tl.scroll_page_down(animate=False, immediate=True)
        if tl.max_scroll_y <= 0 or tl.scroll_y >= tl.max_scroll_y - 2:
            self._follow = True

    def on_mouse_scroll_up(self) -> None:
        self._follow = False

    def on_mouse_scroll_down(self) -> None:
        tl = self._timeline()
        if tl.max_scroll_y <= 0 or tl.scroll_y >= tl.max_scroll_y - 2:
            self._follow = True


_SAVED_LAST_RESORT = None


def _quiet_stdio_logging() -> List:
    """TUI 期间不要把 logging 打到终端，否则会盖住输入框。

    Textual 会换掉 sys.stderr，不能靠 `stream is sys.stderr` 判断。
    FileHandler 也是 StreamHandler 子类，必须留下。
    """
    global _SAVED_LAST_RESORT
    root = logging.getLogger()
    removed = []
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            continue
        if isinstance(handler, logging.StreamHandler):
            root.removeHandler(handler)
            removed.append(handler)
    _SAVED_LAST_RESORT = logging.lastResort
    logging.lastResort = None
    return removed


def _restore_stdio_logging(handlers: List) -> None:
    global _SAVED_LAST_RESORT
    root = logging.getLogger()
    for handler in handlers:
        root.addHandler(handler)
    logging.lastResort = _SAVED_LAST_RESORT
    _SAVED_LAST_RESORT = None


def run_tui(session_id: Optional[str] = None) -> None:
    sid = session_id or datetime.now().strftime("cli-%Y%m%d-%H%M%S")
    SparkTui(sid, os.getcwd()).run()
