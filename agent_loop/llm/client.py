"""三个供应商共用的 OpenAI 兼容调用：重试、流式读取、把响应拼回去。"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import aiohttp

from agent_loop.abort import Abort, RetryCancelledError

# 第一块响应体没到，120 秒放弃。读到第一块之后，整段流再给 10 分钟。
STREAM_OPEN_TIMEOUT_SEC = 120
STREAM_TIMEOUT_SEC = 10 * 60

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
    "余额不足",
    "无可用资源包",
    "请充值",
    '"code":"1113"',
    '"code": "1113"',
)


class LLMHTTPError(RuntimeError):
    """HTTP 失败。message 带上响应正文，欠费不会被 aiohttp 的 Too Many Requests 盖住。"""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body or ""
        super().__init__(f"HTTP {status}: {self.body[:500]}")


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn | tool_use
    finish_reason: str = ""
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


class ChatClient:
    """一家供应商的聊天接口。子类只补这家接口特有的请求字段。"""

    provider_id = ""
    default_model = ""
    api_base_default = ""
    env_name = ""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        api_base: Optional[str] = None,
        timeout: float = STREAM_OPEN_TIMEOUT_SEC,
    ):
        from agent_loop.llm.models import canonical_model, context_window_for, module_for

        chosen = self.default_model if not model else canonical_model(model)
        owner = module_for(chosen)
        if owner.PROVIDER_ID != self.provider_id:
            raise RuntimeError(f"{chosen} belongs to {owner.PROVIDER_ID}")
        self.model = chosen
        self.api_key = (api_key or _env_key(owner)).strip()
        if not self.api_key:
            raise RuntimeError(f"{owner.ENV_NAME} is not set (put it in ~/.permanent/.env)")
        self.api_base = (api_base or owner.API_BASE).rstrip("/")
        # 只约束「等到第一块流式数据」。流开始之后的上限是 STREAM_TIMEOUT_SEC。
        self.timeout = timeout
        self.context_window = context_window_for(self.model)
        # 设了就在每次成功调用后追加一行用量。不设则只留在这次响应上。
        self.usage_path: Optional[Path] = None

    def use(self, model_id: str) -> str:
        from agent_loop.llm.models import canonical_model, context_window_for, module_for

        model_id = canonical_model(model_id)
        owner = module_for(model_id)
        if owner.PROVIDER_ID != self.provider_id:
            raise RuntimeError(f"{model_id} belongs to {owner.PROVIDER_ID}")
        self.model = model_id
        self.context_window = context_window_for(model_id)
        return self.model

    @property
    def supports_images(self) -> bool:
        from agent_loop.llm.models import is_vision

        name = (self.model or "").strip().lower()
        return is_vision(name) or "vision" in name

    def prepare_payload(
        self,
        payload: Dict[str, Any],
        tools: Optional[List[Dict]],
        kwargs: Dict[str, Any],
    ) -> None:
        """子类往 payload 里加本家接口要的字段。"""

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
        }
        self.prepare_payload(payload, tools, kwargs)
        if tools:
            payload["tools"] = tools
        # max_tokens 只在压缩模块的摘要请求里传（限制摘要输出长度）；主对话循环
        # 不传，交给模型自己决定长度。
        if kwargs.get("max_tokens") is not None:
            payload["max_tokens"] = kwargs["max_tokens"]

        on_delta: Optional[Callable[..., None]] = kwargs.get("on_delta")
        last_exc: Optional[BaseException] = None
        for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
            if abort and abort.aborted:
                raise RetryCancelledError()
            try:
                chunks = await self._stream(payload, abort, on_delta=on_delta)
                response = parse_stream_chunks(chunks)
                self._append_usage(response)
                return response
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
                    "[%s] %s，%.1fs 后重试 %s/%s",
                    type(self).__name__,
                    exc,
                    delay,
                    attempt,
                    LLM_MAX_ATTEMPTS - 1,
                )
                await _abortable_sleep(delay, abort)
        raise last_exc  # pragma: no cover

    def _append_usage(self, response: LLMResponse) -> None:
        """把这一次调用的用量追加到 usage_path。摘要和记忆整理走同一个客户端，也会记上。"""
        path = self.usage_path
        if path is None:
            return
        prompt = int(response.prompt_tokens or 0)
        completion = int(response.completion_tokens or 0)
        if prompt == 0 and completion == 0:
            return
        usage = response.usage if isinstance(response.usage, dict) else {}
        details = usage.get("prompt_tokens_details")
        cache_write = 0
        if isinstance(details, dict):
            cache_write = int(details.get("cache_write_tokens") or 0)
        row = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_hit_tokens": int(response.cache_hit_tokens or 0),
            "cache_write_tokens": cache_write,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
        except OSError as exc:
            logging.warning("[llm] could not append usage: %s", exc)

    async def _stream(
        self,
        payload: Dict[str, Any],
        abort: Optional[Abort] = None,
        on_delta: Optional[Callable[..., None]] = None,
    ) -> List[Dict[str, Any]]:
        """让真正的 HTTP 请求和"等待 abort"赛跑，谁先完成就取消另一个——
        这样 Ctrl+C 能立刻打断一个卡住的流式请求，而不用等 aiohttp 超时。
        """
        stream_task = asyncio.create_task(self._stream_once(payload, on_delta=on_delta))
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

    async def _stream_once(
        self,
        payload: Dict[str, Any],
        on_delta: Optional[Callable[..., None]] = None,
    ) -> List[Dict[str, Any]]:
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # total 要盖住「等第一块 + 之后的 10 分钟」，否则 aiohttp 仍会在 120 秒掐掉。
        # sock_read 先按等第一块来；读到第一块后会关掉，改由下面的截止时间收口。
        timeout = aiohttp.ClientTimeout(
            total=self.timeout + STREAM_TIMEOUT_SEC,
            sock_connect=self.timeout,
            sock_read=self.timeout,
        )
        chunks: List[Dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        started = loop.time()
        name = type(self).__name__
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logging.error("[%s] HTTP %s: %s", name, resp.status, body[:500])
                    raise LLMHTTPError(resp.status, body)
                async for obj in _iter_sse_json(
                    resp,
                    open_deadline=started + self.timeout,
                    stream_timeout=STREAM_TIMEOUT_SEC,
                ):
                    chunks.append(obj)
                    if on_delta:
                        _emit_text_delta(obj, on_delta)
        return chunks


def _env_key(owner) -> str:
    import os

    return (os.environ.get(owner.ENV_NAME) or "").strip()


def _emit_text_delta(obj: Dict[str, Any], on_delta: Callable[..., None]) -> None:
    """把 SSE delta 交给 on_delta(text, channel)。

    channel：reasoning（思维链）/ content（正文）/ tool（只有 tool_calls，用来结束等首 token）。
    旧的单参数回调只收 content，其它 channel 丢掉。
    """
    choices = obj.get("choices") or []
    if not choices:
        return
    delta = (choices[0] or {}).get("delta") or {}
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    if reasoning:
        _notify_delta(on_delta, reasoning, "reasoning")
    content = delta.get("content")
    if content:
        _notify_delta(on_delta, content, "content")
    elif not reasoning and delta.get("tool_calls"):
        _notify_delta(on_delta, "", "tool")


def _notify_delta(on_delta: Callable[..., None], text: str, channel: str) -> None:
    try:
        on_delta(text, channel)
    except TypeError:
        if channel == "content" and text:
            on_delta(text)


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


def _relax_read_timeout(resp) -> None:
    """第一块已经到达。取消 aiohttp 按 120 秒空闲掐断的计时，后面改看整段截止时间。"""
    conn = getattr(resp, "connection", None)
    protocol = getattr(conn, "protocol", None) if conn is not None else None
    if protocol is None or not hasattr(protocol, "read_timeout"):
        return
    protocol.read_timeout = None
    start = getattr(protocol, "start_timeout", None)
    if start is not None:
        start()


async def _iter_sse_json(
    resp,
    *,
    open_deadline: float,
    stream_timeout: float,
) -> AsyncIterator[Dict[str, Any]]:
    """把 SSE `data: {...}` 收成 JSON 对象。`data: [DONE]` 结束。

    open_deadline 是从请求开始算的绝对时间：在这之前必须读到第一块响应体。
    读到之后，整段流要在 stream_timeout 秒内收完。
    """
    buf = ""
    opened = False
    loop = asyncio.get_running_loop()
    deadline = open_deadline
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("等待流式输出超过时限" if not opened else "流式输出超过时限")
        try:
            raw = await asyncio.wait_for(resp.content.readany(), remaining)
        except asyncio.TimeoutError:
            raise TimeoutError("等待流式输出超过时限" if not opened else "流式输出超过时限") from None
        if not raw:
            return
        if not opened:
            opened = True
            deadline = loop.time() + stream_timeout
            _relax_read_timeout(resp)
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
                logging.warning("[llm] skip bad sse chunk: %s", payload[:200])


def _cache_hit_tokens(usage: Dict[str, Any]) -> int:
    """DeepSeek 用 prompt_cache_hit_tokens。Kimi 用 cached_tokens，明细在 prompt_tokens_details。"""
    if usage.get("prompt_cache_hit_tokens") is not None:
        return int(usage.get("prompt_cache_hit_tokens") or 0)
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return int(details.get("cached_tokens") or 0)
    if usage.get("cached_tokens") is not None:
        return int(usage.get("cached_tokens") or 0)
    return 0


def _with_usage(response: LLMResponse, usage: Optional[Dict[str, Any]]) -> LLMResponse:
    if not usage:
        return response
    response.usage = usage
    response.prompt_tokens = int(usage.get("prompt_tokens") or 0)
    response.completion_tokens = int(usage.get("completion_tokens") or 0)
    response.cache_hit_tokens = _cache_hit_tokens(usage)
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
    finish_reason = ""
    for data in chunks:
        if data.get("usage"):
            usage = data["usage"]
        choices = data.get("choices") or []
        if not choices:
            continue
        choice = choices[0] or {}
        if choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
        delta = choice.get("delta") or {}
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
    response.finish_reason = finish_reason
    return _with_usage(response, usage)
