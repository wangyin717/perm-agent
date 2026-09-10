"""web_search：路由、格式、空 query、截断。不打真实网络。"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent_loop.runtime.tool_runtime import ToolRuntime
from agent_loop.tools.registry import TOOLS, get_tool
from agent_loop.tools.web_search_tool import (
    AFTER_TRUNCATE_CHARS,
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS_CAP,
    PerplexitySearchError,
    REPLAY,
    _clamp_max_results,
    _hits_from_perplexity,
    _parse_ddgs_envelope,
    after_tool_truncate_output,
    execute,
    format_results,
)


HITS = [
    {
        "title": "Async traits in Rust",
        "url": "https://example.com/async-trait",
        "snippet": "How to write async traits.",
    },
    {
        "title": "RFC",
        "url": "https://example.com/rfc",
        "snippet": "The RFC text.",
    },
]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    async def _blocked_perplexity(*args, **kwargs):
        raise AssertionError("search_perplexity should be mocked")

    async def _blocked_ddgs(*args, **kwargs):
        raise AssertionError("search_ddgs should be mocked")

    monkeypatch.setattr(
        "agent_loop.tools.web_search_tool.search_perplexity", _blocked_perplexity
    )
    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_ddgs", _blocked_ddgs)


def test_replay_is_safe():
    assert REPLAY == "safe"


def test_registered():
    assert "web_search" in TOOLS
    assert get_tool("web_search") is TOOLS["web_search"]


def test_format_results():
    out = format_results(HITS, "rust async trait", backend="perplexity")
    assert out.startswith("[perplexity] query: rust async trait")
    assert "1. Async traits in Rust" in out
    assert "   https://example.com/async-trait" in out
    assert "   How to write async traits." in out
    assert "2. RFC" in out


def test_format_no_results():
    out = format_results([], "zzzz", backend="duckduckgo")
    assert out == "[duckduckgo] query: zzzz\n\nNo results."


def test_clamp_max_results():
    assert _clamp_max_results(None) == DEFAULT_MAX_RESULTS
    assert _clamp_max_results("") == DEFAULT_MAX_RESULTS
    assert _clamp_max_results("nope") == DEFAULT_MAX_RESULTS
    assert _clamp_max_results(0) == 1
    assert _clamp_max_results(3) == 3
    assert _clamp_max_results(99) == MAX_RESULTS_CAP


def test_hits_from_perplexity():
    data = {
        "results": [
            {"title": "A", "url": "https://a.example", "snippet": "aa"},
            {"title": "B", "url": "https://b.example"},
        ]
    }
    hits = _hits_from_perplexity(data)
    assert hits[0]["url"] == "https://a.example"
    assert hits[1]["snippet"] == ""


def test_parse_ddgs_envelope():
    raw = json.dumps({"ok": True, "results": [{"title": "T", "url": "https://t", "snippet": "s"}]})
    hits = _parse_ddgs_envelope(raw)
    assert hits == [{"title": "T", "url": "https://t", "snippet": "s"}]


def test_parse_ddgs_envelope_error():
    with pytest.raises(RuntimeError, match="rate"):
        _parse_ddgs_envelope(json.dumps({"ok": False, "error": "rate limited"}))


def test_no_key_uses_ddgs(monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)

    async def fake_ddgs(query, max_results):
        assert query == "rust async"
        assert max_results == 5
        return HITS

    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_ddgs", fake_ddgs)
    out = asyncio.run(execute({"query": "rust async"}))
    assert out.startswith("[duckduckgo] query: rust async")
    assert "Async traits in Rust" in out


def test_key_uses_perplexity(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "px-test")

    async def fake_pplx(query, max_results, api_key):
        assert api_key == "px-test"
        assert max_results == 3
        return HITS[:1]

    async def fake_ddgs(*args, **kwargs):
        raise AssertionError("ddgs should not run when perplexity succeeds")

    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_perplexity", fake_pplx)
    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_ddgs", fake_ddgs)
    out = asyncio.run(execute({"query": "rust async", "max_results": 3}))
    assert out.startswith("[perplexity] query: rust async")
    assert "RFC" not in out


def test_perplexity_5xx_falls_back(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "px-test")

    async def fake_pplx(*args, **kwargs):
        raise PerplexitySearchError("busy", status=503)

    async def fake_ddgs(query, max_results):
        return HITS

    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_perplexity", fake_pplx)
    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_ddgs", fake_ddgs)
    out = asyncio.run(execute({"query": "rust async"}))
    assert out.startswith("[duckduckgo fallback] query: rust async")
    assert "using duckduckgo" in out
    assert "Async traits in Rust" in out


def test_perplexity_401_fallback_mentions_key(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "bad")

    async def fake_pplx(*args, **kwargs):
        raise PerplexitySearchError("unauthorized", status=401)

    async def fake_ddgs(query, max_results):
        return []

    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_perplexity", fake_pplx)
    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_ddgs", fake_ddgs)
    out = asyncio.run(execute({"query": "rust async"}))
    assert "duckduckgo fallback" in out
    assert "key rejected" in out
    assert "No results." in out


def test_perplexity_400_does_not_fallback(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "px-test")

    async def fake_pplx(*args, **kwargs):
        raise PerplexitySearchError("bad query", status=400)

    async def fake_ddgs(*args, **kwargs):
        raise AssertionError("400 should not fall back")

    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_perplexity", fake_pplx)
    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_ddgs", fake_ddgs)
    with pytest.raises(PerplexitySearchError, match="bad query"):
        asyncio.run(execute({"query": "rust async"}))


def test_both_fail_raises(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "px-test")

    async def fake_pplx(*args, **kwargs):
        raise PerplexitySearchError("down", status=500)

    async def fake_ddgs(*args, **kwargs):
        raise RuntimeError("ddgs rate limited")

    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_perplexity", fake_pplx)
    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_ddgs", fake_ddgs)
    with pytest.raises(RuntimeError, match="ddgs rate limited"):
        asyncio.run(execute({"query": "rust async"}))


def test_empty_query_blocked_by_runtime():
    runtime = ToolRuntime()
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {"name": "web_search", "arguments": '{"query": "  "}'},
            }
        )
    )
    assert result.is_error is True
    assert "query 为空" in result.content


def test_runtime_success(monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)

    async def fake_ddgs(query, max_results):
        return HITS

    monkeypatch.setattr("agent_loop.tools.web_search_tool.search_ddgs", fake_ddgs)
    runtime = ToolRuntime()
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "c1",
                "function": {
                    "name": "web_search",
                    "arguments": '{"query": "rust async"}',
                },
            }
        )
    )
    assert result.is_error is False
    assert "[duckduckgo]" in result.content
    assert result.tool_name == "web_search"


def test_truncate_hook_uses_32k_not_8k():
    short = "x" * 8001
    assert after_tool_truncate_output({"content": short}) is None
    long = "y" * (AFTER_TRUNCATE_CHARS + 10)
    patched = after_tool_truncate_output({"content": long})
    assert patched is not None
    assert patched["content"].endswith("\n...[output truncated]")
    assert len(patched["content"]) == AFTER_TRUNCATE_CHARS + len("\n...[output truncated]")
