"""web_fetch：拦内网、转 HTML、跳转再检查。不打真实外网。"""
from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path

import pytest

from agent_loop.runtime.tool_runtime import ToolRuntime, _lock_path
from agent_loop.tools.registry import TOOLS, get_tool
from agent_loop.tools.web_fetch_tool import (
    AFTER_TRUNCATE_CHARS,
    READ_ONLY,
    REPLAY,
    FetchResponse,
    WebFetchError,
    after_tool_truncate_output,
    assert_public_url,
    body_to_text,
    execute,
    html_to_text,
    is_blocked_ip,
    normalize_url,
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    async def _blocked(url: str):
        raise AssertionError(f"http_get should be mocked, got {url}")

    monkeypatch.setattr("agent_loop.tools.web_fetch_tool.http_get", _blocked)


def test_replay_and_read_only():
    assert REPLAY == "safe"
    assert READ_ONLY is True
    assert "web_fetch" in TOOLS
    assert get_tool("web_fetch") is TOOLS["web_fetch"]
    assert _lock_path(TOOLS["web_fetch"], {"url": "https://example.com"}, ".") is None


def test_normalize_upgrades_http():
    assert normalize_url("http://example.com/a") == "https://example.com/a"


def test_normalize_rejects_file_and_userinfo():
    with pytest.raises(WebFetchError, match="scheme"):
        normalize_url("file:///etc/passwd")
    with pytest.raises(WebFetchError, match="userinfo"):
        normalize_url("https://user:pass@example.com/")


def test_blocked_ips():
    for raw in ("127.0.0.1", "10.0.0.1", "192.168.1.8", "169.254.169.254", "::1"):
        assert is_blocked_ip(ipaddress.ip_address(raw))
    assert not is_blocked_ip(ipaddress.ip_address("8.8.8.8"))


def test_assert_public_rejects_literals():
    async def _run():
        with pytest.raises(WebFetchError, match="non-public"):
            await assert_public_url("https://127.0.0.1/")
        with pytest.raises(WebFetchError, match="non-public"):
            await assert_public_url("https://10.1.2.3/secret")
        with pytest.raises(WebFetchError, match="localhost"):
            await assert_public_url("https://localhost/admin")

    asyncio.run(_run())


def test_html_to_text_strips_script():
    html = """
    <html><head><title>T</title><script>alert(1)</script></head>
    <body>
      <h1>Hello</h1>
      <p>World <a href="https://ex.com">link</a></p>
      <style>.x{}</style>
    </body></html>
    """
    text = html_to_text(html)
    assert "alert" not in text
    assert ".x{}" not in text
    assert "Hello" in text
    assert "World" in text
    assert "https://ex.com" in text


def test_pdf_rejected(monkeypatch):
    async def fake_get(url: str):
        return FetchResponse(200, {"Content-Type": "application/pdf"}, b"%PDF-1.4")

    monkeypatch.setattr("agent_loop.tools.web_fetch_tool.http_get", fake_get)
    monkeypatch.setattr(
        "agent_loop.tools.web_fetch_tool.assert_public_url",
        lambda url: asyncio.sleep(0),
    )
    with pytest.raises(WebFetchError, match="unsupported content type"):
        asyncio.run(execute({"url": "https://example.com/file.pdf"}))


def test_fetch_html_success(monkeypatch):
    html = b"<html><body><h1>Doc</h1><p>Install rustc.</p></body></html>"

    async def fake_get(url: str):
        assert url.startswith("https://example.com")
        return FetchResponse(200, {"Content-Type": "text/html; charset=utf-8"}, html)

    monkeypatch.setattr("agent_loop.tools.web_fetch_tool.http_get", fake_get)
    monkeypatch.setattr(
        "agent_loop.tools.web_fetch_tool.assert_public_url",
        lambda url: asyncio.sleep(0),
    )
    out = asyncio.run(execute({"url": "https://example.com/doc"}))
    assert out.startswith("[web_fetch] https://example.com/doc")
    assert "Doc" in out
    assert "Install rustc." in out
    assert "<p>" not in out


def test_empty_html_errors(monkeypatch):
    async def fake_get(url: str):
        return FetchResponse(200, {"Content-Type": "text/html"}, b"<html><script>x</script></html>")

    monkeypatch.setattr("agent_loop.tools.web_fetch_tool.http_get", fake_get)
    monkeypatch.setattr(
        "agent_loop.tools.web_fetch_tool.assert_public_url",
        lambda url: asyncio.sleep(0),
    )
    with pytest.raises(WebFetchError, match="little text"):
        asyncio.run(execute({"url": "https://example.com/app"}))


def test_redirect_to_private_is_blocked(monkeypatch):
    hops = {"n": 0}

    async def fake_get(url: str):
        hops["n"] += 1
        if "example.com" in url:
            return FetchResponse(302, {"Location": "https://127.0.0.1/secret"}, b"")
        return FetchResponse(200, {"Content-Type": "text/plain"}, b"should not fetch")

    async def fake_ssrf(url: str):
        if "127.0.0.1" in url:
            raise WebFetchError("blocked non-public address: 127.0.0.1")

    monkeypatch.setattr("agent_loop.tools.web_fetch_tool.http_get", fake_get)
    monkeypatch.setattr("agent_loop.tools.web_fetch_tool.assert_public_url", fake_ssrf)
    with pytest.raises(WebFetchError, match="non-public"):
        asyncio.run(execute({"url": "https://example.com/go"}))
    assert hops["n"] == 1


def test_http_error(monkeypatch):
    async def fake_get(url: str):
        return FetchResponse(404, {"Content-Type": "text/plain"}, b"missing")

    monkeypatch.setattr("agent_loop.tools.web_fetch_tool.http_get", fake_get)
    monkeypatch.setattr(
        "agent_loop.tools.web_fetch_tool.assert_public_url",
        lambda url: asyncio.sleep(0),
    )
    with pytest.raises(WebFetchError, match="HTTP 404"):
        asyncio.run(execute({"url": "https://example.com/nope"}))


def test_empty_url_blocked_by_runtime():
    runtime = ToolRuntime()
    result = asyncio.run(
        runtime.call_tool(
            {"id": "c1", "function": {"name": "web_fetch", "arguments": '{"url": "  "}'}}
        )
    )
    assert result.is_error is True
    assert "url 为空" in result.content


def test_runtime_success(monkeypatch):
    async def fake_get(url: str):
        return FetchResponse(200, {"Content-Type": "text/plain"}, b"hello page")

    monkeypatch.setattr("agent_loop.tools.web_fetch_tool.http_get", fake_get)
    monkeypatch.setattr(
        "agent_loop.tools.web_fetch_tool.assert_public_url",
        lambda url: asyncio.sleep(0),
    )
    runtime = ToolRuntime()
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {
                    "name": "web_fetch",
                    "arguments": '{"url": "https://example.com/"}',
                },
            }
        )
    )
    assert result.is_error is False
    assert "[web_fetch]" in result.content
    assert "hello page" in result.content


def test_truncate_hook_uses_64k():
    assert after_tool_truncate_output({"content": "x" * 8001}) is None
    long = "y" * (AFTER_TRUNCATE_CHARS + 5)
    patched = after_tool_truncate_output({"content": long})
    assert patched["content"].endswith("\n...[output truncated]")


def test_plain_text_passthrough():
    text = body_to_text(b"just text", "text/plain")
    assert text == "just text"


def test_saves_full_page_returns_stub(tmp_path, monkeypatch):
    body = "\n".join([f"line {i}" for i in range(80)]).encode()

    async def fake_get(url: str):
        return FetchResponse(200, {"Content-Type": "text/plain"}, body)

    monkeypatch.setattr("agent_loop.tools.web_fetch_tool.http_get", fake_get)
    monkeypatch.setattr(
        "agent_loop.tools.web_fetch_tool.assert_public_url",
        lambda url: asyncio.sleep(0),
    )
    session_dir = tmp_path / ".agent" / "sessions" / "s1"
    out = asyncio.run(
        execute(
            {"url": "https://example.com/long"},
            workspace=str(tmp_path),
            session_dir=str(session_dir),
        )
    )
    assert "saved:" in out
    assert "chars:" in out
    assert "line 0" in out
    assert "line 79" not in out
    assert "grep or read the saved path" in out
    fetch_dir = session_dir / "workspace" / "tools_result" / "web_fetch"
    files = list(fetch_dir.iterdir())
    assert len(files) == 1
    saved = files[0].read_text(encoding="utf-8")
    assert "line 79" in saved
    rel = out.split("saved:", 1)[1].splitlines()[0].strip()
    assert (tmp_path / rel).read_text(encoding="utf-8") == saved
