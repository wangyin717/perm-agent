"""拉取一个公开 http(s) URL，转成可读文本。自己 GET，不走搜索后端。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp

NAME = "web_fetch"
REPLAY = "safe"
READ_ONLY = True

AFTER_TRUNCATE_CHARS = 64000
MAX_URL_LENGTH = 2000
MAX_REDIRECTS = 10
MAX_CONTENT_BYTES = 5 * 1024 * 1024
TIMEOUT_SEC = 30
USER_AGENT = "Mozilla/5.0 (compatible; Permanent/0.1)"
PREVIEW_LINES = 30
PREVIEW_CHARS = 2000

_REDIRECT_STATUS = {301, 302, 303, 307, 308}
_TEXT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "application/json",
    "application/xml",
    "text/xml",
)
_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "object",
    "embed",
    "head",
}
_BLOCK_TAGS = {
    "p",
    "div",
    "br",
    "tr",
    "li",
    "section",
    "article",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pre",
    "blockquote",
    "ul",
    "ol",
}


def before_tool_deny_empty_url(event: Dict[str, Any]):
    url = str(((event.get("args") or {}).get("url") or "")).strip()
    if not url:
        return {"block": {"reason": "url 为空"}}
    return None


def after_tool_truncate_output(event: Dict[str, Any]):
    content = event.get("content") or ""
    if len(content) <= AFTER_TRUNCATE_CHARS:
        return None
    return {"content": content[:AFTER_TRUNCATE_CHARS] + "\n...[output truncated]"}


BEFORE_HOOKS = [before_tool_deny_empty_url]
AFTER_HOOKS = [after_tool_truncate_output]


class WebFetchError(ValueError):
    """读页失败，由 ToolRuntime 收成 is_error。"""


async def execute(args: Dict[str, Any], sandbox=None, workspace=None, session_dir=None) -> str:
    raw = str(args.get("url") or "").strip()
    if not raw:
        raise WebFetchError("url 为空")
    url = normalize_url(raw)
    body, content_type, final_url = await fetch_following_redirects(url)
    text = body_to_text(body, content_type)
    if not text.strip():
        raise WebFetchError(
            "page has little text; it is probably JavaScript-rendered. "
            f"url={final_url}"
        )
    full = f"[web_fetch] {final_url}\n\n{text}"
    if not session_dir:
        return full
    saved = _write_snapshot(session_dir, workspace, final_url, full)
    return _stub(final_url, saved, full)


def _write_snapshot(session_dir: str, workspace: Optional[str], url: str, full: str) -> str:
    short_id = uuid.uuid4().hex[:12]
    dest = Path(session_dir) / "workspace" / "tools_result" / "web_fetch" / short_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(full, encoding="utf-8")
    root = Path(workspace or ".").resolve()
    try:
        return dest.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(dest)


def _stub(url: str, saved: str, full: str) -> str:
    lines = full.splitlines()
    preview_lines = lines[:PREVIEW_LINES]
    preview = "\n".join(preview_lines)
    if len(preview) > PREVIEW_CHARS:
        preview = preview[:PREVIEW_CHARS].rstrip() + "\n..."
    elif len(lines) > PREVIEW_LINES:
        preview = preview.rstrip() + "\n..."
    return (
        f"[web_fetch] {url}\n"
        f"saved: {saved}\n"
        f"chars: {len(full)}\n\n"
        f"{preview}\n\n"
        "To find something on this page, grep or read the saved path "
        "(pass path= that file). The file is UTF-8 text: a [web_fetch] url "
        "header, then the body — not raw JSON. Do not bash it, do not "
        "json.load the whole file, and do not bash curl the URL again."
    )


def normalize_url(raw: str) -> str:
    if len(raw) > MAX_URL_LENGTH:
        raise WebFetchError(f"url too long ({len(raw)} > {MAX_URL_LENGTH})")
    parsed = urlparse(raw.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme in ("", "http"):
        parsed = parsed._replace(scheme="https")
        scheme = "https"
    if scheme != "https":
        raise WebFetchError(f"unsupported URL scheme: {scheme or '(none)'}")
    if parsed.username or parsed.password:
        raise WebFetchError("URL must not include userinfo")
    host = (parsed.hostname or "").strip().rstrip(".")
    if not host:
        raise WebFetchError("URL has no host")
    # 丢掉 fragment，不发到服务器
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if ip.version == 6 and getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().rstrip(".")
    if not host:
        raise WebFetchError("URL has no host")
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise WebFetchError(f"blocked non-public host: {host}")
    port = parsed.port or 443
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if is_blocked_ip(ip):
            raise WebFetchError(f"blocked non-public address: {host}")
        return
    try:
        infos = await _resolve(host, port)
    except OSError as exc:
        raise WebFetchError(f"DNS failed for {host}: {exc}") from exc
    if not infos:
        raise WebFetchError(f"DNS returned no addresses for {host}")
    for found in infos:
        if is_blocked_ip(found):
            raise WebFetchError(f"blocked non-public address: {host} -> {found}")


async def _resolve(host: str, port: int) -> List[ipaddress._BaseAddress]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips: List[ipaddress._BaseAddress] = []
    seen = set()
    for info in infos:
        addr = info[4][0]
        if addr in seen:
            continue
        seen.add(addr)
        try:
            ips.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    return ips


class FetchResponse:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    @property
    def location(self) -> str:
        return (self.headers.get("location") or "").strip()

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").strip()


async def http_get(url: str) -> FetchResponse:
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/markdown, text/plain;q=0.9, text/html;q=0.8, */*;q=0.1",
        "Accept-Language": "en-US,en;q=0.8",
    }
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=False, headers=headers) as resp:
            header_map = {k: v for k, v in resp.headers.items()}
            chunks: List[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > MAX_CONTENT_BYTES:
                    raise WebFetchError(
                        f"response larger than {MAX_CONTENT_BYTES} bytes"
                    )
                chunks.append(chunk)
            return FetchResponse(resp.status, header_map, b"".join(chunks))


async def fetch_following_redirects(url: str) -> Tuple[bytes, str, str]:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        await assert_public_url(current)
        resp = await http_get(current)
        if resp.status in _REDIRECT_STATUS:
            if not resp.location:
                raise WebFetchError(f"HTTP {resp.status} redirect with no Location")
            nxt = urljoin(current, resp.location)
            current = normalize_url(nxt)
            continue
        if resp.status >= 400:
            raise WebFetchError(f"HTTP {resp.status} for {current}")
        content_type = resp.content_type.lower()
        _assert_supported_type(content_type, resp.body)
        return resp.body, content_type, current
    raise WebFetchError(f"too many redirects (>{MAX_REDIRECTS})")


def _assert_supported_type(content_type: str, body: bytes) -> None:
    mime = content_type.split(";", 1)[0].strip().lower()
    if not mime:
        return
    if mime.startswith("image/") or mime.startswith("audio/") or mime.startswith("video/"):
        raise WebFetchError(f"unsupported content type: {mime}")
    if mime in {"application/pdf", "application/octet-stream", "application/zip"}:
        raise WebFetchError(f"unsupported content type: {mime}")
    if mime in _TEXT_TYPES or mime.startswith("text/"):
        return
    # 未知类型：若看起来像文本就放行
    if b"\x00" in body[:8000]:
        raise WebFetchError(f"unsupported binary content type: {mime}")


def body_to_text(body: bytes, content_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    text = _decode(body, content_type)
    if mime in {"text/html", "application/xhtml+xml"} or (
        not mime and text.lstrip()[:15].lower().startswith(("<!doctype", "<html", "<head"))
    ):
        return html_to_text(text)
    return text.strip()


def _decode(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    lower = content_type.lower()
    if "charset=" in lower:
        charset = lower.split("charset=", 1)[1].split(";")[0].strip().strip("\"'")
    try:
        return body.decode(charset)
    except LookupError:
        return body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._parts: List[str] = []
        self._href: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in _BLOCK_TAGS or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("\n")
        if tag in {"h1", "h2", "h3"}:
            self._parts.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self._parts.append("- ")
        if tag == "a":
            self._href = dict(attrs).get("href")
        if tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag == "a" and self._href:
            href = self._href.strip()
            if href:
                self._parts.append(f" ({href})")
            self._href = None
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        collapsed = " ".join(data.split())
        if collapsed:
            self._parts.append(collapsed + " ")


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    text = "".join(parser._parts)
    lines = [line.strip() for line in text.splitlines()]
    out: List[str] = []
    blank = False
    for line in lines:
        if not line:
            if out and not blank:
                out.append("")
            blank = True
            continue
        blank = False
        out.append(line)
    return "\n".join(out).strip()
