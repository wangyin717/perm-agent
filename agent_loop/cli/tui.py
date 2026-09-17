"""Grok 风格时间线：用户顶栏、◆ 工具行、中间穿插 markdown。"""

from __future__ import annotations

import os
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
from textual.containers import Horizontal, VerticalGroup, VerticalScroll
from textual.markup import escape as markup_escape
from textual.selection import Selection
from textual.visual import RenderOptions
from textual.widgets import Input, Static

from agent_loop import events
from agent_loop.cli.app import CliDeps
from agent_loop.loop import ReactAgentLoop
from agent_loop.tools.diff_view import clip_diff_line
from agent_loop.paths import session_log_path_for
from agent_loop.session_log import SessionLog

HELP = """commands:
  /help      this list
  /exit      quit (/quit same)
  /new       new session (blank history)
  /session   show session id and log path
keys: Enter send  Esc abort  Ctrl+Q quit"""

_IDLE = "idle  /help  Enter send  Esc abort  Ctrl+Q quit"
_ARG_MAX = 64
_STEP_TOOLS_MAX = 3


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
    if name == "/session":
        return "session"
    return None


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
    return name


def _clip_arg(text: str, limit: int = _ARG_MAX) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


_BLUE = "#1a73e8"
_TEXT = "#1c1c1e"
_MUTED = "#6e6e73"
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
        "markdown.list": Style(),
        "markdown.item.number": Style(color=_TEXT),
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
        return f"[bold]{parts[0]}[/] [#6e6e73]{arg}[/]"
    return f"[bold]{text}[/]"


def _mark(text: str) -> str:
    """过程行：菱形灰色，不跟动词抢。"""
    return f"[#6e6e73]◆[/]  {text}"


def _thinking_line(dt: float) -> str:
    return _mark(f"[bold]Thinking[/][#6e6e73]… {dt:.1f}s[/]")


def _thought_line(dt: float) -> str:
    return _mark(f"[bold]Thought[/] [#6e6e73]for {dt:.1f}s[/]")


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
    return f"[#6e6e73]{text}[/]"


_PYGMENT_COLORS = {
    Token.Keyword: "#7c3aed",
    Token.Keyword.Constant: "#7c3aed",
    Token.Name.Builtin: "#7c3aed",
    Token.Name.Function: "#1c1c1e",
    Token.Name.Class: "#1c1c1e",
    Token.Name.Decorator: "#1967d2",
    Token.String: "#c2410c",
    Token.String.Doc: "#c2410c",
    Token.Comment: "#6e6e73",
    Token.Number: "#1967d2",
    Token.Operator: "#3c3c43",
}


def _pygment_style(tok) -> str:
    while tok is not None:
        if tok in _PYGMENT_COLORS:
            return _PYGMENT_COLORS[tok]
        tok = getattr(tok, "parent", None)
    return "#1c1c1e"


def highlight_code_line(code: str, filename: str = "") -> Text:
    code = clip_diff_line(code.replace("\n", ""))
    try:
        from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
        from pygments.util import ClassNotFound
    except ImportError:
        return Text(code, style="#1c1c1e")
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
            return Text(code, style="#1c1c1e")
    out = Text()
    for tok, val in lexer.get_tokens(code + "\n"):
        val = val.replace("\n", "")
        if val:
            out.append(val, _pygment_style(tok))
    return out if out.plain else Text(code, style="#1c1c1e")


class UserBanner(Static):
    DEFAULT_CSS = """
    UserBanner {
        background: #e8e8ea;
        color: #1c1c1e;
        padding: 1 2;
        width: 100%;
        margin-bottom: 1;
    }
    """


class TimelineRow(Static):
    DEFAULT_CSS = """
    TimelineRow {
        width: 100%;
        height: 1;
        margin: 0;
        padding: 0 2;
        color: #3c3c43;
    }
    TimelineRow.thought { color: #1c1c1e; }
    TimelineRow.tool { color: #1c1c1e; }
    TimelineRow.edit { color: #1c1c1e; }
    TimelineRow.muted {
        height: auto;
        color: #6e6e73;
    }
    """


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
        margin: 1 3 1 3;
        background: #f4f4f5;
        color: #1c1c1e;
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


class SparkTui(App):
    TITLE = "spark-agent"
    ALLOW_SELECT = True
    CSS = """
    Screen {
        layout: vertical;
        background: #f4f4f5;
        color: #1c1c1e;
    }
    #chrome {
        height: 1;
        padding: 0 1;
        color: #6e6e73;
        background: #ececee;
    }
    #timeline {
        height: 1fr;
        background: #f4f4f5;
        overflow-x: hidden;
        overflow-y: scroll;
    }
    #timeline > .turn {
        height: auto;
        width: 1fr;
        layout: vertical;
        margin: 0;
        padding: 0;
    }
    #timeline TimelineRow {
        height: 1;
        margin: 0;
        padding: 0 2;
    }
    #timeline TimelineRow.muted {
        height: auto;
    }
    #timeline Prose {
        height: auto;
        margin: 1 3;
        padding: 0;
        background: #f4f4f5;
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
    #prompt-wrap {
        dock: bottom;
        height: 3;
        margin: 0 1;
        padding: 0 1;
        background: #ffffff;
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
        background: #ffffff;
        border: none;
        padding: 0 1 0 0;
    }
    #prompt:focus {
        border: none;
        background-tint: 0%;
    }
    #prompt > .input--placeholder {
        color: #8e8e93;
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
        self.loop = ReactAgentLoop(CliDeps(), None, None)
        self._busy = False
        self._unsub = None
        self._pending_user = None
        self._stream_buf = ""
        self._turn: Optional[VerticalGroup] = None
        self._md: Optional[Prose] = None
        self._thought: Optional[TimelineRow] = None
        self._think_t0: Optional[float] = None
        self._had_reasoning = False
        self._busy_t0: Optional[float] = None
        self._aborting = False
        self._context_used: Optional[int] = None
        self._context_limit: Optional[int] = None
        self._compactions: List[Dict] = []
        self._tools_shown = 0
        self._ellipsis = False
        self._follow = True
        self._last_stream_paint = 0.0

    def compose(self) -> ComposeResult:
        yield Static("", id="chrome")
        yield Timeline(id="timeline")
        with Horizontal(id="prompt-wrap"):
            yield Static("›", id="prompt-mark")
            yield Input(placeholder="Message or /help", id="prompt", compact=True)

    def on_mount(self) -> None:
        events.print_to_stdout = False
        self.console.push_theme(_PROSE_THEME)
        self._unsub = events.subscribe(self._on_event)
        path = session_log_path_for(self.workspace, self.session_id)
        self.query_one("#chrome", Static).update(f"spark-agent  {self.session_id}")
        self.set_interval(0.1, self._tick_think)
        self._replay_log(path)
        self.query_one("#prompt", Input).focus()

    def on_unmount(self) -> None:
        events.print_to_stdout = True
        try:
            self.console.pop_theme()
        except Exception:
            pass
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _timeline(self) -> Timeline:
        return self.query_one("#timeline", VerticalScroll)

    def _start_turn(self, text: str) -> VerticalGroup:
        self._end_think()
        turn = VerticalGroup(classes="turn")
        self._timeline().mount(turn)
        turn.mount(UserBanner(f"›  {text}"))
        self._turn = turn
        self._md = None
        self._stream_buf = ""
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
        self._thought = None
        self._think_t0 = None
        self._had_reasoning = False
        if row is not None:
            if had and dt is not None:
                row.update(_thought_line(dt))
            else:
                row.remove()
        elif had and dt is not None and self._turn is not None:
            self._turn.mount(TimelineRow(_thought_line(dt), classes="thought"))
            self._scroll_follow()

    def _add_tool_row(self, name: str, args: Optional[Dict] = None) -> None:
        if self._turn is None:
            return
        if self._tools_shown >= _STEP_TOOLS_MAX:
            if not self._ellipsis:
                self._turn.mount(TimelineRow(_mark("[#6e6e73]…[/]"), classes="tool"))
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
            self._start_turn(text)
        elif kind == "assistant_delta":
            channel = str(event.get("channel") or "content")
            piece = str(event.get("text") or "")
            if channel == "reasoning":
                if piece:
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
            self._follow = True
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
            self._add_tool_row(name, args)
            self._scroll_follow()
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
        elif kind == "error":
            self._end_think()
            if self._turn is not None:
                self._turn.mount(
                    TimelineRow(_mark(f"error  {event.get('text') or ''}"), classes="muted")
                )

    def _set_status(self, text: str) -> None:
        return

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return
        cmd = parse_slash(text)
        if cmd:
            await self._run_command(cmd)
            return
        if text.startswith("/"):
            self._timeline().mount(TimelineRow(_mark("unknown command. type /help"), classes="muted"))
            return
        if self._busy:
            self.loop.inbox.push_steer(text)
            if self._turn is not None:
                self._turn.mount(UserBanner(f"›  {text}"))
            return
        self._busy = True
        self._aborting = False
        self._busy_t0 = time.monotonic()
        self._pending_user = text
        self._start_turn(text)
        self.run_worker(self._run_turn(text), exclusive=True, group="turn")

    async def _run_command(self, cmd: str) -> None:
        if cmd == "exit":
            self.exit()
            return
        if cmd == "help":
            self._timeline().mount(Prose(HELP))
            return
        if cmd == "session":
            path = session_log_path_for(self.workspace, self.session_id)
            self._timeline().mount(TimelineRow(_mark(str(self.session_id)), classes="muted"))
            self._timeline().mount(TimelineRow(_mark(str(path)), classes="muted"))
            return
        if cmd == "new":
            abort = getattr(self.loop, "_abort", None)
            if abort is not None:
                abort.abort()
            self.session_id = datetime.now().strftime("cli-%Y%m%d-%H%M%S")
            self.loop = ReactAgentLoop(CliDeps(), None, None)
            self._busy = False
            self._aborting = False
            self._turn = None
            self._md = None
            self._thought = None
            self._think_t0 = None
            self._had_reasoning = False
            self._busy_t0 = None
            self._context_used = None
            self._context_limit = None
            self._compactions = []
            timeline = self._timeline()
            await timeline.remove_children()
            self.query_one("#chrome", Static).update(f"spark-agent  {self.session_id}")
            path = session_log_path_for(self.workspace, self.session_id)
            timeline.mount(TimelineRow(_mark(str(path)), classes="muted"))
            self._set_status(_IDLE)
            self.query_one("#prompt", Input).focus()

    async def _run_turn(self, query: str) -> None:
        uad = {
            "sessionId": self.session_id,
            "workspace": self.workspace,
            "install_sigint": False,
        }
        try:
            await self.loop._run_loop(query, uad)
        except Exception as exc:
            events.emit("error", text=str(exc) or type(exc).__name__)
        finally:
            self._finish_turn()
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
        self._end_think()
        self._busy = False
        self._busy_t0 = None
        self._aborting = False
        if dt is None:
            self._set_status(_IDLE)
            return
        text = self._finish_line(dt)
        if self._turn is not None:
            self._turn.mount(TimelineRow(_muted(text), classes="muted"))
            self._scroll_follow()
        self._set_status(_IDLE)

    def action_copy_selection(self) -> None:
        text = self.screen.get_selected_text()
        if not text:
            raise SkipAction()
        self.copy_to_clipboard(text)

    def action_abort_turn(self) -> None:
        abort = getattr(self.loop, "_abort", None)
        if abort is not None:
            abort.abort()
            self._aborting = True
            self._end_think()
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


def run_tui(session_id: Optional[str] = None) -> None:
    sid = session_id or datetime.now().strftime("cli-%Y%m%d-%H%M%S")
    SparkTui(sid, os.getcwd()).run()
