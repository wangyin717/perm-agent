"""最小 LLM 客户端。先用 DeepSeek 做联调（OpenAI 兼容 /chat/completions）。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from agent_loop.abort import Abort, RetryCancelledError

# FIXME: 临时本地联调用，正式环境请改成只读环境变量
API_KEY = "sk-a6c363b264d342a8bd3e62fb67b03a1e"
API_BASE = "https://api.deepseek.com"
MODEL = "deepseek-v4-flash"

# 第 1 次不算重试；最多再试 2 次
LLM_MAX_ATTEMPTS = 3
MAX_RETRY_DELAY_SEC = 60
_BILLING_MARKERS = (
    "insufficient_quota",
    "quota exceeded",
    "billing",
    "out of budget",
    "payment required",
    "欠费",
)


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn | tool_use


def is_retryable_llm_error(exc: BaseException) -> bool:
    if isinstance(exc, RetryCancelledError):
        return False
    msg = str(exc).lower()
    if any(marker in msg for marker in _BILLING_MARKERS):
        return False
    status = getattr(exc, "status", None)
    if status in (400, 401, 402):
        return False
    if status in (408, 409, 429) or (isinstance(status, int) and status >= 500):
        return True
    if isinstance(
        exc,
        (asyncio.TimeoutError, aiohttp.ServerTimeoutError, aiohttp.ClientConnectorError),
    ):
        return True
    return False


def retry_delay_seconds(exc: BaseException, retry_index: int) -> float:
    """retry_index=0 表示第一次重试。服务端 Retry-After 超过上限则抛错。"""
    headers = getattr(exc, "headers", None)
    if headers is not None and hasattr(headers, "get"):
        ms = headers.get("retry-after-ms")
        if ms is not None:
            delay = float(ms) / 1000.0
        else:
            raw = headers.get("Retry-After") or headers.get("retry-after")
            delay = float(raw) if raw is not None else None
        if delay is not None:
            if delay > MAX_RETRY_DELAY_SEC:
                raise RuntimeError(
                    f"Server requested {delay:.0f}s retry delay (max: {MAX_RETRY_DELAY_SEC}s). {exc}"
                )
            return delay
    delay = min(0.5 * (2**retry_index), 8)
    return delay * (1 - random.random() * 0.25)


class DeepSeekLLM:
    """DeepSeek Chat Completions。call() 的返回值给 React 循环分支用。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        api_base: Optional[str] = None,
        timeout: float = 120,
    ):
        self.api_key = api_key or API_KEY
        self.model = model or MODEL
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.timeout = timeout

    async def call(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        abort: Optional[Abort] = None,
        **kwargs,
    ) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_exc: Optional[BaseException] = None
        for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
            if abort and abort.aborted:
                raise RetryCancelledError()
            try:
                data = await self._post(payload, abort)
                return parse_chat_response(data)
            except RetryCancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                if not is_retryable_llm_error(exc) or attempt == LLM_MAX_ATTEMPTS:
                    raise
                retry_index = attempt - 1
                delay = retry_delay_seconds(exc, retry_index)
                logging.warning(
                    "[DeepSeekLLM] %s，%.1fs 后重试 %s/%s",
                    exc,
                    delay,
                    attempt,
                    LLM_MAX_ATTEMPTS - 1,
                )
                await _abortable_sleep(delay, abort)
        raise last_exc  # pragma: no cover

    async def _post(self, payload: Dict[str, Any], abort: Optional[Abort] = None) -> Dict[str, Any]:
        post_task = asyncio.create_task(self._post_once(payload))
        if abort is None:
            return await post_task
        abort_task = asyncio.create_task(abort.wait())
        done, pending = await asyncio.wait(
            {post_task, abort_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if abort.aborted:
            raise RetryCancelledError()
        return post_task.result()

    async def _post_once(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logging.error("[DeepSeekLLM] HTTP %s: %s", resp.status, body[:500])
                    resp.raise_for_status()
                return await resp.json(content_type=None)


async def _abortable_sleep(seconds: float, abort: Optional[Abort]) -> None:
    if abort is None:
        await asyncio.sleep(seconds)
        return
    if abort.aborted:
        raise RetryCancelledError()
    try:
        await asyncio.wait_for(abort.wait(), timeout=seconds)
        raise RetryCancelledError()
    except asyncio.TimeoutError:
        return


def parse_chat_response(data: Dict[str, Any]) -> LLMResponse:
    """把 OpenAI 兼容的 chat.completions JSON 收成 LLMResponse。"""
    message = ((data.get("choices") or [{}])[0].get("message")) or {}
    text = message.get("content") or ""
    tool_calls = list(message.get("tool_calls") or [])
    if tool_calls:
        return LLMResponse(text=text, tool_calls=tool_calls, stop_reason="tool_use")
    return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn")
