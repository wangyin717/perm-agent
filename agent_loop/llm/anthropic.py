"""Anthropic Messages 协议。官方 Claude、以及提供该格式的 DeepSeek、Kimi 都走这里。

现有模型仍用 client.py 的 Chat Completions。这个类不登记模型名单，地址和密钥由调用方传入。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from agent_loop.abort import Abort, RetryCancelledError
from agent_loop.llm.client import (
    LLMHTTPError,
    LLMResponse,
    STREAM_OPEN_TIMEOUT_SEC,
    STREAM_TIMEOUT_SEC,
    _abortable_sleep,
    _iter_sse_json,
    _max_attempts,
    _notify_delta,
    is_retryable_llm_error,
    retry_delay_seconds,
)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_API_BASE = "https://api.anthropic.com"
# Messages 接口要求带 max_tokens。主循环不传，这里给一个足够长的默认。
DEFAULT_MAX_TOKENS = 8192
DEFAULT_CONTEXT_WINDOW = 200_000


class AnthropicClient:
    """把现有的 OpenAI 形状消息翻成 Messages，再把流式结果翻回 LLMResponse。"""

    def __init__(
        self,
        api_key: str,
        model: str,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = STREAM_OPEN_TIMEOUT_SEC,
        context_window: int = DEFAULT_CONTEXT_WINDOW,
    ):
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise RuntimeError("Anthropic api_key is empty")
        self.model = (model or "").strip()
        if not self.model:
            raise RuntimeError("Anthropic model is empty")
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.timeout = timeout
        self.context_window = context_window
        self.supports_images = True

    async def call(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        abort: Optional[Abort] = None,
        **kwargs,
    ) -> LLMResponse:
        max_tokens = kwargs.get("max_tokens")
        if max_tokens is None:
            max_tokens = DEFAULT_MAX_TOKENS
        payload = build_payload(self.model, messages, tools, int(max_tokens))
        on_delta: Optional[Callable[..., None]] = kwargs.get("on_delta")
        attempt = 0
        while True:
            attempt += 1
            if abort and abort.aborted:
                raise RetryCancelledError()
            try:
                events = await self._stream(payload, abort, on_delta=on_delta)
                return parse_anthropic_events(events)
            except RetryCancelledError:
                raise
            except Exception as exc:
                limit = _max_attempts(exc)
                if not is_retryable_llm_error(exc) or attempt >= limit:
                    raise
                delay = retry_delay_seconds(exc, attempt - 1)
                logging.warning(
                    "[Anthropic] %s，%.1fs 后重试 %s/%s",
                    exc,
                    delay,
                    attempt,
                    limit - 1,
                )
                await _abortable_sleep(delay, abort)

    async def _stream(
        self,
        payload: Dict[str, Any],
        abort: Optional[Abort] = None,
        on_delta: Optional[Callable[..., None]] = None,
    ) -> List[Dict[str, Any]]:
        # 和 ChatClient 一样，流和取消谁先完成听谁的。
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
        url = f"{self.api_base}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(
            total=self.timeout + STREAM_TIMEOUT_SEC,
            sock_connect=self.timeout,
            sock_read=self.timeout,
        )
        events: List[Dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logging.error("[Anthropic] HTTP %s: %s", resp.status, body[:500])
                    raise LLMHTTPError(resp.status, body, resp.headers)
                async for obj in _iter_sse_json(
                    resp,
                    open_deadline=started + self.timeout,
                    stream_timeout=STREAM_TIMEOUT_SEC,
                ):
                    events.append(obj)
                    if on_delta:
                        _emit_anthropic_delta(obj, on_delta)
                    if obj.get("type") == "message_stop":
                        break
        return events


def build_payload(
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict]],
    max_tokens: int,
) -> Dict[str, Any]:
    system, converted = to_anthropic_messages(messages)
    if not converted:
        raise ValueError("没有可发送的消息")
    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": converted,
        "stream": True,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = [_tool_schema(item) for item in tools]
    return payload


def to_anthropic_messages(
    messages: List[Dict[str, Any]],
) -> tuple:
    """OpenAI 形状 → (system 文本, Messages 列表)。

    同一次调用的工具结果收成一条 user。紧跟的读图用户消息并进对应的 tool_result，
    不再插在两条工具结果中间。
    """
    system_parts: List[str] = []
    out: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []

    def flush_tools() -> None:
        if not pending:
            return
        _append(out, "user", list(pending))
        pending.clear()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            text = _plain_text(msg.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            pending.append(_tool_result_block(msg))
            continue
        if role == "user" and pending and _is_image_followup(msg):
            _attach_images(pending[-1], msg.get("content"))
            continue
        flush_tools()
        if role == "user":
            _append(out, "user", _user_blocks(msg.get("content")))
        elif role == "assistant":
            _append(out, "assistant", _assistant_blocks(msg))
    flush_tools()
    system = "\n\n".join(system_parts)
    return system, out


def parse_anthropic_events(events: List[Dict[str, Any]]) -> LLMResponse:
    """把 Messages 的 SSE 事件收成和 Chat Completions 一样的 LLMResponse。"""
    text_parts: List[str] = []
    tools: Dict[int, Dict[str, str]] = {}
    usage: Dict[str, Any] = {}
    stop = ""
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "message_start":
            _merge_usage(usage, (event.get("message") or {}).get("usage") or {})
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                stop = str(delta["stop_reason"])
            _merge_usage(usage, event.get("usage") or {})
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                tools[int(event.get("index") or 0)] = {
                    "id": str(block.get("id") or ""),
                    "name": str(block.get("name") or ""),
                    "partial": "",
                }
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                text_parts.append(str(delta["text"]))
            elif delta.get("type") == "input_json_delta":
                slot = tools.setdefault(
                    int(event.get("index") or 0),
                    {"id": "", "name": "", "partial": ""},
                )
                slot["partial"] += str(delta.get("partial_json") or "")
    tool_calls = [_openai_tool_call(tools[index]) for index in sorted(tools)]
    text = "".join(text_parts)
    if tool_calls:
        response = LLMResponse(text=text, tool_calls=tool_calls, stop_reason="tool_use")
    else:
        response = LLMResponse(text=text, tool_calls=[], stop_reason="end_turn")
    response.finish_reason = stop
    return _with_anthropic_usage(response, usage)


def _emit_anthropic_delta(event: Dict[str, Any], on_delta: Callable[..., None]) -> None:
    kind = event.get("type")
    if kind == "content_block_start":
        block = event.get("content_block") or {}
        if block.get("type") == "tool_use":
            _notify_delta(on_delta, "", "tool")
        return
    if kind != "content_block_delta":
        return
    delta = event.get("delta") or {}
    if delta.get("type") == "text_delta" and delta.get("text"):
        _notify_delta(on_delta, str(delta["text"]), "content")
    elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
        _notify_delta(on_delta, str(delta["thinking"]), "reasoning")


def _tool_schema(item: Dict[str, Any]) -> Dict[str, Any]:
    if item.get("input_schema"):
        return {
            "name": item.get("name") or "",
            "description": item.get("description") or "",
            "input_schema": item["input_schema"],
        }
    fn = item.get("function") or item
    schema = fn.get("parameters") or fn.get("input_schema") or {
        "type": "object",
        "properties": {},
    }
    return {
        "name": fn.get("name") or "",
        "description": fn.get("description") or "",
        "input_schema": schema,
    }


def _tool_result_block(msg: Dict[str, Any]) -> Dict[str, Any]:
    text = _plain_text(msg.get("content"))
    return {
        "type": "tool_result",
        "tool_use_id": msg.get("tool_call_id") or "",
        "content": text,
    }


def _attach_images(block: Dict[str, Any], content: Any) -> None:
    images = _image_blocks(content)
    if not images:
        return
    text = block.get("content")
    parts: List[Dict[str, Any]] = []
    if isinstance(text, str) and text:
        parts.append({"type": "text", "text": text})
    elif isinstance(text, list):
        parts.extend(text)
    parts.extend(images)
    block["content"] = parts


def _assistant_blocks(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    text = _plain_text(msg.get("content"))
    if text:
        blocks.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = fn.get("name") or call.get("name") or ""
        arguments = fn.get("arguments")
        if arguments is None:
            arguments = call.get("input") or {}
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                parsed = {}
        elif isinstance(arguments, dict):
            parsed = arguments
        else:
            parsed = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or "",
                "name": name,
                "input": parsed,
            }
        )
    return blocks


def _user_blocks(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    blocks: List[Dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            blocks.append({"type": "text", "text": str(item["text"])})
        elif item.get("type") == "image_url":
            image = _image_block((item.get("image_url") or {}).get("url") or "")
            if image:
                blocks.append(image)
        elif item.get("type") == "image":
            blocks.append(item)
    return blocks


def _image_blocks(content: Any) -> List[Dict[str, Any]]:
    return [item for item in _user_blocks(content) if item.get("type") == "image"]


def _image_block(url: str) -> Optional[Dict[str, Any]]:
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        mime = header[5:].split(";", 1)[0] or "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": data},
        }
    if url.startswith("http://") or url.startswith("https://"):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _is_image_followup(msg: Dict[str, Any]) -> bool:
    content = msg.get("content")
    if not isinstance(content, list) or not content:
        return False
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        return False
    return str(first.get("text") or "").startswith("[image from read:")


def _plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts)
    return ""


def _append(out: List[Dict[str, Any]], role: str, blocks: List[Dict[str, Any]]) -> None:
    if not blocks:
        return
    if out and out[-1]["role"] == role:
        prev = out[-1]["content"]
        if isinstance(prev, list):
            merged = list(prev)
        elif isinstance(prev, str) and prev:
            merged = [{"type": "text", "text": prev}]
        else:
            merged = []
        out[-1]["content"] = merged + blocks
        return
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        out.append({"role": role, "content": blocks[0]["text"]})
    else:
        out.append({"role": role, "content": blocks})


def _openai_tool_call(slot: Dict[str, str]) -> Dict[str, Any]:
    arguments = slot.get("partial") or "{}"
    return {
        "id": slot.get("id") or "",
        "type": "function",
        "function": {"name": slot.get("name") or "", "arguments": arguments},
    }


def _merge_usage(dest: Dict[str, Any], usage: Dict[str, Any]) -> None:
    for key, value in usage.items():
        if value is not None:
            dest[key] = value


def _with_anthropic_usage(response: LLMResponse, usage: Dict[str, Any]) -> LLMResponse:
    if not usage:
        return response
    fresh = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cache_read_input_tokens") or 0)
    created = int(usage.get("cache_creation_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    response.usage = usage
    response.prompt_tokens = fresh + cached + created
    response.completion_tokens = output
    response.cache_hit_tokens = cached
    response.cache_miss_tokens = fresh + created
    return response
