"""最小 LLM 客户端。先用 DeepSeek 做联调（OpenAI 兼容 /chat/completions）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from agent_loop.abort import Abort, RetryCancelledError

# FIXME: 临时本地联调用，正式环境请改成只读环境变量
API_KEY = "sk-a6c363b264d342a8bd3e62fb67b03a1e"
API_BASE = "https://api.deepseek.com"
MODEL = "deepseek-v4-flash"
CONTEXT_WINDOWS = {
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
}
DEFAULT_CONTEXT_WINDOW = 1_000_000

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
    usage: Optional[Dict[str, Any]] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0


def is_retryable_llm_error(exc: BaseException) -> bool:
    """判断一次失败要不要重试。三条互斥的判断，按顺序：
    1. 用户主动取消（Ctrl+C）——永远不重试，直接向上抛
    2. 文案像欠费/quota——重试也没用，直接失败，省得傻等
    3. HTTP 状态码：400/401/402 是请求本身或鉴权有问题，重试不会变好；
       408/409/429/5xx 或网络层超时/连接错误，是暂时性问题，值得重试
    其余（比如 403/404）默认不重试。
    """
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
    """retry_index=0 表示第一次重试。服务端 Retry-After 超过上限则抛错。

    优先听服务端的 Retry-After（明确知道该等多久，不用猜）；没有的话退回本地的
    指数退避（0.5s → 1s → 2s...，封顶 8s），加一点随机抖动避免多个请求同时醒来。
    """
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
                # 服务端要求等太久：宁可让上层看到明确的失败，也不要静默卡住整个 loop。
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
        self.context_window = CONTEXT_WINDOWS.get(self.model, DEFAULT_CONTEXT_WINDOW)

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
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # max_tokens 只在压缩模块的摘要请求里传（限制摘要输出长度）；主对话循环
        # 不传，交给模型自己决定长度。
        if kwargs.get("max_tokens") is not None:
            payload["max_tokens"] = kwargs["max_tokens"]

        last_exc: Optional[BaseException] = None
        for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
            if abort and abort.aborted:
                raise RetryCancelledError()
            try:
                chunks = await self._stream(payload, abort)
                return parse_stream_chunks(chunks)
            except RetryCancelledError:
                # Ctrl+C 打断的，不算"可重试的服务端错误"，直接向上传播。
                raise
            except Exception as exc:
                last_exc = exc
                # 欠费/400/401/402 等不可重试错误，或已经打满次数：不再等，直接抛出去
                # 让调用方（agent_loop）决定怎么处理。
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

    async def _stream(
        self, payload: Dict[str, Any], abort: Optional[Abort] = None
    ) -> List[Dict[str, Any]]:
        """让真正的 HTTP 请求和"等待 abort"赛跑，谁先完成就取消另一个——
        这样 Ctrl+C 能立刻打断一个卡住的流式请求，而不用等 aiohttp 超时。
        """
        stream_task = asyncio.create_task(self._stream_once(payload))
        if abort is None:
            return await stream_task
        abort_task = asyncio.create_task(abort.wait())
        done, pending = await asyncio.wait(
            {stream_task, abort_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if abort.aborted:
            raise RetryCancelledError()
        return stream_task.result()

    async def _stream_once(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        chunks: List[Dict[str, Any]] = []
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logging.error("[DeepSeekLLM] HTTP %s: %s", resp.status, body[:500])
                    resp.raise_for_status()
                async for obj in _iter_sse_json(resp):
                    chunks.append(obj)
        return chunks


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


async def _iter_sse_json(resp) -> AsyncIterator[Dict[str, Any]]:
    """把 SSE `data: {...}` 收成 JSON 对象。`data: [DONE]` 结束。"""
    buf = ""
    async for raw in resp.content.iter_any():
        buf += raw.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                logging.warning("[DeepSeekLLM] skip bad sse chunk: %s", payload[:200])


def _with_usage(response: LLMResponse, usage: Optional[Dict[str, Any]]) -> LLMResponse:
    if not usage:
        return response
    response.usage = usage
    response.prompt_tokens = int(usage.get("prompt_tokens") or 0)
    response.completion_tokens = int(usage.get("completion_tokens") or 0)
    response.cache_hit_tokens = int(usage.get("prompt_cache_hit_tokens") or 0)
    response.cache_miss_tokens = int(usage.get("prompt_cache_miss_tokens") or 0)
    return response


def parse_chat_response(data: Dict[str, Any]) -> LLMResponse:
    """把 OpenAI 兼容的 chat.completions JSON 收成 LLMResponse。"""
    message = ((data.get("choices") or [{}])[0].get("message")) or {}
    text = message.get("content") or ""
    tool_calls = list(message.get("tool_calls") or [])
    if tool_calls:
        response = LLMResponse(text=text, tool_calls=tool_calls, stop_reason="tool_use")
    else:
        response = LLMResponse(text=text, tool_calls=[], stop_reason="end_turn")
    return _with_usage(response, data.get("usage"))


def parse_stream_chunks(chunks: List[Dict[str, Any]]) -> LLMResponse:
    """拼 SSE 增量：文本、tool_calls、最后一包 usage。

    流式返回里，同一个 tool_call 的 name/arguments 是分成好几个 chunk 逐字符/逐
    片段发过来的，用 delta 里的 index 对齐到同一个 slot 上累加拼接，不能假设一个
    chunk 就是完整的一个 tool_call。usage 只有最后一个 chunk 才带（服务端在流
    结束时补发），所以要遍历完所有 chunk 才能拿到。
    """
    text_parts: List[str] = []
    tools: Dict[int, Dict[str, Any]] = {}
    usage: Optional[Dict[str, Any]] = None
    for data in chunks:
        if data.get("usage"):
            usage = data["usage"]
        choices = data.get("choices") or []
        if not choices:
            continue
        delta = (choices[0] or {}).get("delta") or {}
        content = delta.get("content")
        if content:
            text_parts.append(content)
        for item in delta.get("tool_calls") or []:
            idx = int(item.get("index") or 0)
            slot = tools.setdefault(
                idx,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if item.get("id"):
                slot["id"] = item["id"]
            if item.get("type"):
                slot["type"] = item["type"]
            fn = item.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]
    tool_calls = [tools[i] for i in sorted(tools)]
    text = "".join(text_parts)
    if tool_calls:
        response = LLMResponse(text=text, tool_calls=tool_calls, stop_reason="tool_use")
    else:
        response = LLMResponse(text=text, tool_calls=[], stop_reason="end_turn")
    return _with_usage(response, usage)
