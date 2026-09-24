"""day 浅色和 night 深色。颜色分别对齐 GrokDay、GrokNight。"""

from __future__ import annotations

from dataclasses import dataclass

from textual.theme import Theme


@dataclass(frozen=True)
class Palette:
    name: str
    page: str
    text: str
    secondary: str
    muted: str
    faint: str
    blue: str
    code: str
    h1: str
    h2: str
    h3: str
    h4: str
    h5: str
    h6: str
    banner: str
    border: str
    border_focus: str
    menu: str
    menu_on: str
    hover: str
    row_hover: str
    row_on: str
    input: str
    cursor_bg: str
    cursor_fg: str
    diff_add: str
    diff_del: str
    splash_border: str
    mark_rest: str
    dark: bool

    def variables(self) -> dict[str, str]:
        return {
            "page": self.page,
            "text": self.text,
            "secondary": self.secondary,
            "muted": self.muted,
            "faint": self.faint,
            "blue": self.blue,
            "code": self.code,
            "h1": self.h1,
            "banner": self.banner,
            "border": self.border,
            "border-focus": self.border_focus,
            "menu": self.menu,
            "menu-on": self.menu_on,
            "hover": self.hover,
            "row-hover": self.row_hover,
            "row-on": self.row_on,
            "input": self.input,
            "cursor-bg": self.cursor_bg,
            "cursor-fg": self.cursor_fg,
            "diff-add": self.diff_add,
            "diff-del": self.diff_del,
            "splash-border": self.splash_border,
            "mark-rest": self.mark_rest,
        }

    def textual(self) -> Theme:
        return Theme(
            name=self.name,
            primary=self.blue,
            foreground=self.secondary,
            background=self.page,
            surface=self.page,
            dark=self.dark,
            variables=self.variables(),
        )


DAY = Palette(
    name="day",
    page="#f5f5f5",
    text="#262626",
    secondary="#444444",
    muted="#767676",
    faint="#8e8e93",
    blue="#2F64D2",
    code="#2F64D2",
    h1="#0A8E70",
    h2="#2F64D2",
    h3="#6C3EB2",
    h4="#626262",
    h5="#767676",
    h6="#8E8E8E",
    banner="#dedede",
    border="#c7c7cc",
    border_focus="#8e8e93",
    menu="#e6e6e8",
    menu_on="#d4d4d8",
    hover="#c8c8c8",
    row_hover="#e8e8ea",
    row_on="#d8d8dc",
    input="#1d1d1f",
    cursor_bg="#000000",
    cursor_fg="#ffffff",
    diff_add="#daf2dc",
    diff_del="#f5dade",
    splash_border="#d5d5d8",
    mark_rest="#c7c7cc",
    dark=False,
)

NIGHT = Palette(
    name="night",
    page="#0a0a0a",
    text="#e1e1e1",
    secondary="#c8c8c8",
    muted="#6c6c6c",
    faint="#5a5a5a",
    blue="#7aa6da",
    code="#3A95AB",
    h1="#1abc9c",
    h2="#7aa2f7",
    h3="#9d7cd8",
    h4="#787878",
    h5="#6c6c6c",
    h6="#5a5a5a",
    banner="#242424",
    border="#323237",
    border_focus="#505058",
    menu="#242424",
    menu_on="#2c2c2c",
    hover="#2c2c2c",
    row_hover="#2c2c2c",
    row_on="#363636",
    input="#e1e1e1",
    cursor_bg="#e1e1e1",
    cursor_fg="#0a0a0a",
    diff_add="#063806",
    diff_del="#420e14",
    splash_border="#323237",
    mark_rest="#414141",
    dark=True,
)

PALETTES = {"day": DAY, "night": NIGHT}
_ALIASES = {"grokday": "day", "groknight": "night"}
_current = DAY


def canonical_theme(name: str) -> str:
    name = _ALIASES.get(name, name)
    return name if name in PALETTES else DAY.name


def palette() -> Palette:
    return _current


def set_palette(name: str) -> Palette:
    global _current
    _current = PALETTES[canonical_theme(name)]
    return _current


def configured_theme() -> str:
    from agent_loop.plugins.config import load_config

    raw = load_config().get("theme")
    if isinstance(raw, str):
        return canonical_theme(raw)
    return DAY.name


def save_theme(name: str) -> str:
    from agent_loop.plugins.config import set_setting

    name = canonical_theme(name)
    set_palette(name)
    set_setting("theme", name)
    return name
