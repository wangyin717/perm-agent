"""用户贴图：整段绝对路径 / 剪贴板位图 → 存 chats/{id}/input/images/。

不扫散文里的路径。对齐 Grok：整段都是路径 token 才当 drop。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from agent_loop.tools.read_tool import MAX_IMAGE_BYTES, sniff_image

PLACEHOLDER_RE = re.compile(r"\[Image #(\d+)\]")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def starts_with_path_anchor(token: str) -> bool:
    s = token.strip()
    if s.startswith("file://") or s.startswith("/") or s.startswith("~/"):
        return True
    if len(s) >= 3 and s[0].isalpha() and s[1:3] == ":\\":
        return True
    if s.startswith("\\\\"):
        return True
    return False


def _strip_quotes(token: str) -> str:
    t = token.strip()
    if len(t) < 2:
        return t
    left, right = t[0], t[-1]
    if left in "'\"" and left == right:
        return t[1:-1].strip()
    if left == "\u2018" and right == "\u2019":
        return t[1:-1].strip()
    if left == "\u201c" and right == "\u201d":
        return t[1:-1].strip()
    return t


def _unescape_posix(token: str) -> str:
    if "\\" not in token:
        return token
    out: List[str] = []
    i = 0
    while i < len(token):
        if token[i] == "\\" and i + 1 < len(token):
            out.append(token[i + 1])
            i += 2
            continue
        out.append(token[i])
        i += 1
    return "".join(out)


def _normalize_path_token(token: str) -> str:
    return _unescape_posix(_strip_quotes(token.strip())).strip()


def token_to_path(token: str) -> Optional[Path]:
    raw = _normalize_path_token(token)
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        path = unquote(parsed.path or "")
        if sys.platform == "win32" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        raw = path
    raw = os.path.expanduser(raw)
    if not raw:
        return None
    try:
        return Path(raw)
    except (OSError, ValueError):
        return None


def _space_split_line(line: str) -> List[str]:
    return [
        p
        for p in re.findall(
            r'"[^"]*"|\'[^\']*\'|\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019|\S+',
            line,
        )
        if p
    ]


def try_read_dropped_path(token: str) -> Optional[Tuple[str, Path]]:
    """返回 ('image', path) 或 ('text', path)。整段无法解析则 None。"""
    trimmed = _normalize_path_token(token)
    is_file_url = trimmed.startswith("file://")
    if not is_file_url and not starts_with_path_anchor(trimmed):
        return None
    path = token_to_path(token)
    if path is None:
        return None
    if not path.as_posix() or path.as_posix() == "/":
        return None
    try:
        data = path.read_bytes() if path.is_file() else b""
    except OSError:
        data = b""
    sniffed = sniff_image(data) if data else None
    if sniffed is not None and path.suffix.lower() in IMAGE_EXTS:
        return ("image", path.resolve())
    if not is_file_url and not path.exists():
        return None
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    return ("text", resolved)


def try_read_dropped_paths(text: str) -> Optional[List[Tuple[str, Path]]]:
    """整段都是路径才返回 list；否则 None（当普通文字粘贴）。"""
    trimmed = (text or "").strip()
    if not trimmed:
        return None
    normalized = trimmed.replace("\r\n", "\n").replace("\r", "\n")
    result: List[Tuple[str, Path]] = []
    for line in normalized.split("\n"):
        line = line.strip()
        if not line:
            continue
        whole = try_read_dropped_path(line)
        if whole is not None:
            result.append(whole)
            continue
        tokens = _space_split_line(line)
        if not tokens:
            continue
        resolved = [try_read_dropped_path(t) for t in tokens]
        if any(item is None for item in resolved):
            return None
        result.extend(item for item in resolved if item is not None)
    return result or None


def persist_image_bytes(data: bytes, mime: str, session_dir: Path) -> Path:
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image too large ({len(data)} bytes)")
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(mime, ".png")
    dest = Path(session_dir) / "input" / "images" / f"{uuid.uuid4().hex[:12]}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest.resolve()


def persist_image_file(src: Path, session_dir: Path) -> Tuple[Path, str]:
    data = src.read_bytes()
    sniffed = sniff_image(data)
    if sniffed is None:
        raise ValueError("not an image")
    mime, _ext = sniffed
    return persist_image_bytes(data, mime, session_dir), mime


def read_clipboard_image_bytes() -> Optional[Tuple[bytes, str]]:
    """macOS 剪贴板位图。其它平台先不读图。"""
    if sys.platform != "darwin":
        return None
    dest = Path("/tmp") / f"spark-paste-{uuid.uuid4().hex[:8]}.png"
    quoted = json.dumps(str(dest))
    script = (
        f"try\n"
        f"  set png_data to the clipboard as «class PNGf»\n"
        f"  set the_file to POSIX file {quoted}\n"
        f"  set f to open for access the_file with write permission\n"
        f"  set eof f to 0\n"
        f"  write png_data to f\n"
        f"  close access f\n"
        f"  return \"ok\"\n"
        f"on error\n"
        f"  try\n"
        f"    close access POSIX file {quoted}\n"
        f"  end try\n"
        f"  return \"fail\"\n"
        f"end try"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=8,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or "ok" not in (proc.stdout or ""):
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    try:
        data = dest.read_bytes()
    except OSError:
        return None
    finally:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
    sniffed = sniff_image(data)
    if not sniffed:
        return None
    return data, sniffed[0]


def next_image_number(text: str, draft: List[Dict]) -> int:
    nums = [int(n) for n in PLACEHOLDER_RE.findall(text or "")]
    nums.extend(int(item.get("n") or 0) for item in draft)
    return (max(nums) if nums else 0) + 1


def placeholders_in_text(text: str) -> List[int]:
    return [int(n) for n in PLACEHOLDER_RE.findall(text or "")]


def placeholder_spans(text: str) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in PLACEHOLDER_RE.finditer(text or "")]


def chip_for_backspace(text: str, pos: int) -> Optional[Tuple[int, int]]:
    """光标在 chip 内或紧跟其后时，退格删掉整枚。"""
    for start, end in placeholder_spans(text):
        if start < pos <= end:
            return (start, end)
    return None


def chip_for_delete(text: str, pos: int) -> Optional[Tuple[int, int]]:
    """光标在 chip 内或正对开头时，Delete 删掉整枚。"""
    for start, end in placeholder_spans(text):
        if start <= pos < end:
            return (start, end)
    return None


def expand_range_to_chips(text: str, start: int, end: int) -> Tuple[int, int]:
    lo, hi = sorted((start, end))
    for a, b in placeholder_spans(text):
        if a < hi and b > lo:
            lo = min(lo, a)
            hi = max(hi, b)
    return lo, hi


def snap_cursor_out_of_chip(text: str, pos: int) -> int:
    for start, end in placeholder_spans(text):
        if start < pos < end:
            if pos - start <= end - pos:
                return start
            return end
    return pos
