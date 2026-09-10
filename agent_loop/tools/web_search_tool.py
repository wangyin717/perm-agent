"""网上搜索。有 PERPLEXITY_API_KEY 走 Search API，否则（或它挂了）走 DuckDuckGo。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from typing import Any, Dict, List, Optional

import aiohttp

NAME = "web_search"
REPLAY = "safe"
READ_ONLY = True

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 10
AFTER_TRUNCATE_CHARS = 32000
PERPLEXITY_URL = "https://api.perplexity.ai/search"
PERPLEXITY_TIMEOUT_SEC = 60
DDGS_TIMEOUT_SEC = 30
_TERMINATE_GRACE_SEC = 1.0


class PerplexitySearchError(Exception):
    """Search API 失败。status 为 None 表示网络/超时等没拿到 HTTP 码。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status

    def should_fallback(self) -> bool:
        if self.status is None:
            return True
        return self.status in (401, 403, 408, 429) or self.status >= 500


def before_tool_deny_empty_query(event: Dict[str, Any]):
    query = str(((event.get("args") or {}).get("query") or "")).strip()
    if not query:
        return {"block": {"reason": "query 为空"}}
    return None


def after_tool_truncate_output(event: Dict[str, Any]):
    content = event.get("content") or ""
    if len(content) <= AFTER_TRUNCATE_CHARS:
        return None
    return {"content": content[:AFTER_TRUNCATE_CHARS] + "\n...[output truncated]"}


BEFORE_HOOKS = [before_tool_deny_empty_query]
AFTER_HOOKS = [after_tool_truncate_output]


def perplexity_key() -> str:
    return (os.environ.get("PERPLEXITY_API_KEY") or "").strip()


def _clamp_max_results(raw: Any) -> int:
    if raw is None or raw == "":
        return DEFAULT_MAX_RESULTS
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESULTS
    return max(1, min(n, MAX_RESULTS_CAP))


def format_results(
    hits: List[Dict[str, str]],
    query: str,
    backend: str,
    note: str = "",
) -> str:
    header = f"[{backend}] query: {query}"
    if note:
        header += f"\n{note}"
    if not hits:
        return f"{header}\n\nNo results."
    blocks = []
    for i, hit in enumerate(hits, start=1):
        title = (hit.get("title") or "").strip() or "(no title)"
        url = (hit.get("url") or "").strip()
        snippet = (hit.get("snippet") or "").strip()
        lines = [f"{i}. {title}"]
        if url:
            lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {snippet}")
        blocks.append("\n".join(lines))
    return header + "\n\n" + "\n\n".join(blocks)


async def execute(args: Dict[str, Any], sandbox=None, workspace=None, session_dir=None) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query 为空")
    max_results = _clamp_max_results(args.get("max_results"))
    key = perplexity_key()
    if key:
        try:
            hits = await search_perplexity(query, max_results, key)
            return format_results(hits, query, backend="perplexity")
        except PerplexitySearchError as exc:
            if not exc.should_fallback():
                raise
            logging.warning("[web_search] perplexity failed (%s), falling back to duckduckgo", exc)
            hits = await search_ddgs(query, max_results)
            note = _fallback_note(exc)
            return format_results(hits, query, backend="duckduckgo fallback", note=note)
    hits = await search_ddgs(query, max_results)
    return format_results(hits, query, backend="duckduckgo")


def _fallback_note(exc: PerplexitySearchError) -> str:
    if exc.status in (401, 403):
        return "perplexity key rejected; using duckduckgo"
    detail = str(exc).strip().replace("\n", " ")[:200]
    return f"perplexity failed ({detail or type(exc).__name__}); using duckduckgo"


def _hits_from_perplexity(data: Any) -> List[Dict[str, str]]:
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise PerplexitySearchError("unexpected Search API response")
    hits = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        hits.append(
            {
                "title": str(row.get("title") or ""),
                "url": str(row.get("url") or ""),
                "snippet": str(row.get("snippet") or ""),
            }
        )
    return hits


async def search_perplexity(query: str, max_results: int, api_key: str) -> List[Dict[str, str]]:
    payload = {"query": query, "max_results": max_results}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=PERPLEXITY_TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(PERPLEXITY_URL, json=payload, headers=headers) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise PerplexitySearchError(
                        (body or "").strip()[:300] or f"HTTP {resp.status}",
                        status=resp.status,
                    )
                try:
                    data = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise PerplexitySearchError("Search API returned non-JSON") from exc
    except PerplexitySearchError:
        raise
    except Exception as exc:
        raise PerplexitySearchError(str(exc) or type(exc).__name__) from exc
    return _hits_from_perplexity(data)


async def search_ddgs(query: str, max_results: int) -> List[Dict[str, str]]:
    return await asyncio.to_thread(_search_ddgs_bounded, query, max_results)


def _search_ddgs_bounded(query: str, max_results: int) -> List[Dict[str, str]]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([p for p in sys.path if p])
    extra: Dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_loop.tools.web_search_ddgs_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        **extra,
    )
    payload = json.dumps({"query": query, "max_results": max_results})
    try:
        out, err = proc.communicate(payload, timeout=DDGS_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        _terminate_and_reap(proc)
        raise TimeoutError(f"DuckDuckGo search timed out after {DDGS_TIMEOUT_SEC}s")
    if proc.returncode not in (0, None) and not (out or "").strip():
        detail = (err or "").strip()[:200]
        raise RuntimeError(detail or f"DuckDuckGo worker exited {proc.returncode}")
    return _parse_ddgs_envelope(out or "")


def _terminate_and_reap(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform != "win32" and proc.pid:
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SEC)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if sys.platform != "win32" and proc.pid:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SEC)
    except subprocess.TimeoutExpired:
        logging.warning("[web_search] ddgs worker pid=%s did not exit after kill", proc.pid)


def _parse_ddgs_envelope(raw: str) -> List[Dict[str, str]]:
    raw = raw.strip()
    if not raw:
        raise RuntimeError("DuckDuckGo worker returned no output")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DuckDuckGo worker returned invalid JSON: {raw[:200]!r}") from exc
    if not isinstance(envelope, dict):
        raise RuntimeError("DuckDuckGo worker returned an invalid envelope")
    if not envelope.get("ok"):
        raise RuntimeError(str(envelope.get("error") or "DuckDuckGo search failed"))
    rows = envelope.get("results") or []
    if not isinstance(rows, list):
        raise RuntimeError("DuckDuckGo worker returned non-list results")
    hits = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        hits.append(
            {
                "title": str(row.get("title") or ""),
                "url": str(row.get("url") or ""),
                "snippet": str(row.get("snippet") or ""),
            }
        )
    return hits
