"""拼 SSE 增量和 usage。"""
import json

from agent_loop.llm.client import (
    _emit_text_delta,
    parse_chat_response,
    parse_stream_chunks,
)
from agent_loop.llm.deepseek import DeepSeek
from agent_loop.llm.models import canonical_model, make_client, save_model
from agent_loop.llm.moonshot import Moonshot
from agent_loop.llm.zhipuai import ZhipuAI
from agent_loop.plugins.config import load_config, set_setting


def test_model_names_and_saved_choice_keep_other_settings():
    assert canonical_model("deepseek-v4-flash") == "deepseek-flash"
    assert canonical_model("deepseek-v4-pro") == "deepseek-v4-pro"
    assert canonical_model("nope") == "deepseek-flash"
    set_setting("plugins", {"browser-use": False})
    assert save_model("deepseek-v4-pro") == "deepseek-v4-pro"
    data = load_config()
    assert data["model"] == "deepseek-v4-pro"
    assert data["plugins"] == {"browser-use": False}
    llm = DeepSeek(api_key="sk-test", model="deepseek-v4-flash")
    assert llm.model == "deepseek-flash"
    assert llm.supports_images is True
    assert llm.use("deepseek-v4-pro") == "deepseek-v4-pro"
    assert llm.supports_images is False
    assert llm.context_window == 1_000_000


def test_glm_switches_endpoint_and_keeps_thinking_on(monkeypatch):
    import asyncio

    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
    llm = ZhipuAI()
    assert llm.model == "glm-5.3-flash"
    assert llm.api_key == "zhipu-test"
    assert llm.api_base == "https://open.bigmodel.cn/api/paas/v4"
    assert llm.supports_images is True
    assert llm.context_window == 1_000_000
    assert llm.use("glm-5.3") == "glm-5.3"
    assert llm.supports_images is False
    seen = {}

    async def fake_stream(payload, abort=None, on_delta=None):
        seen["payload"] = payload
        return [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]

    llm._stream = fake_stream
    asyncio.run(
        llm.call(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "bash", "parameters": {}}}],
        )
    )
    payload = seen["payload"]
    assert payload["model"] == "glm-5.3"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"
    asyncio.run(llm.call([{"role": "user", "content": "hi"}], max_tokens=400))
    assert seen["payload"]["reasoning_effort"] == "low"
    assert payload["tool_stream"] is True
    assert "stream_options" not in payload
    assert "tool_choice" not in payload
    other = make_client("deepseek-flash")
    assert isinstance(other, DeepSeek)
    assert other.api_key == "ds-test"
    assert other.api_base == "https://api.deepseek.com"


def test_kimi_switches_endpoint_and_keeps_fixed_sampling(monkeypatch):
    import asyncio

    monkeypatch.setenv("MOONSHOT_API_KEY", "moon-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
    llm = Moonshot()
    assert llm.model == "kimi-k3"
    assert llm.api_key == "moon-test"
    assert llm.api_base == "https://api.moonshot.cn/v1"
    assert llm.supports_images is True
    assert llm.context_window == 1_000_000
    seen = {}

    async def fake_stream(payload, abort=None, on_delta=None):
        seen["payload"] = payload
        return [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]

    llm._stream = fake_stream
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    asyncio.run(llm.call([{"role": "user", "content": "hi"}], tools=tools))
    payload = seen["payload"]
    assert payload["model"] == "kimi-k3"
    assert payload["reasoning_effort"] == "max"
    assert payload["tool_choice"] == "auto"
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert payload["stream_options"] == {"include_usage": True}
    assert "thinking" not in payload
    asyncio.run(llm.call([{"role": "user", "content": "hi"}], max_tokens=400))
    assert seen["payload"]["reasoning_effort"] == "low"
    assert llm.use("kimi-k2.7-code") == "kimi-k2.7-code"
    assert llm.api_key == "moon-test"
    assert llm.supports_images is True
    assert llm.context_window == 256_000
    asyncio.run(llm.call([{"role": "user", "content": "hi"}], tools=tools))
    payload = seen["payload"]
    assert payload["model"] == "kimi-k2.7-code"
    assert payload["tool_choice"] == "auto"
    assert "reasoning_effort" not in payload
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert payload["stream_options"] == {"include_usage": True}
    assert "thinking" not in payload


def test_zhipu_balance_429_is_not_retried():
    from agent_loop.llm.client import LLMHTTPError, is_retryable_llm_error

    body = '{"error":{"code":"1113","message":"余额不足或无可用资源包,请充值。"}}'
    billing = LLMHTTPError(429, body)
    assert "余额不足" in str(billing)
    assert is_retryable_llm_error(billing) is False
    assert is_retryable_llm_error(LLMHTTPError(429, "rate limit")) is True


def test_emit_text_delta_ignores_tool_chunks():
    seen = []

    def capture(text, channel="content"):
        seen.append((text, channel))

    _emit_text_delta({"choices": [{"delta": {"content": "hi"}}]}, capture)
    _emit_text_delta({"choices": [{"delta": {"tool_calls": [{}]}}]}, capture)
    _emit_text_delta({"choices": []}, capture)
    assert seen == [("hi", "content"), ("", "tool")]


def test_emit_reasoning_delta_before_content():
    seen = []

    def capture(text, channel="content"):
        seen.append((text, channel))

    _emit_text_delta({"choices": [{"delta": {"reasoning_content": "hmm"}}]}, capture)
    _emit_text_delta({"choices": [{"delta": {"content": "42"}}]}, capture)
    assert seen == [("hmm", "reasoning"), ("42", "content")]


def test_emit_text_delta_one_arg_callback_keeps_content():
    seen = []
    _emit_text_delta({"choices": [{"delta": {"content": "hi"}}]}, seen.append)
    _emit_text_delta({"choices": [{"delta": {"reasoning_content": "skip"}}]}, seen.append)
    _emit_text_delta({"choices": [{"delta": {"tool_calls": [{}]}}]}, seen.append)
    assert seen == ["hi"]


def test_parse_stream_text_and_usage():
    chunks = [
        {"choices": [{"delta": {"content": "hello "}}]},
        {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "prompt_cache_hit_tokens": 8,
                "prompt_cache_miss_tokens": 2,
            },
        },
    ]
    response = parse_stream_chunks(chunks)
    assert response.text == "hello world"
    assert response.stop_reason == "end_turn"
    assert response.prompt_tokens == 10
    assert response.completion_tokens == 2
    assert response.cache_hit_tokens == 8
    assert response.cache_miss_tokens == 2
    assert response.usage["total_tokens"] == 12


def test_parse_stream_kimi_cache_usage(tmp_path):
    chunks = [
        {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 19,
                "completion_tokens": 13,
                "total_tokens": 32,
                "cached_tokens": 12,
                "prompt_tokens_details": {
                    "cached_tokens": 12,
                    "cache_write_tokens": 4,
                },
            },
        },
    ]
    response = parse_stream_chunks(chunks)
    assert response.prompt_tokens == 19
    assert response.completion_tokens == 13
    assert response.cache_hit_tokens == 12
    assert response.cache_miss_tokens == 0

    llm = DeepSeek(api_key="sk-test")
    llm.usage_path = tmp_path / "usage.jsonl"
    llm._append_usage(response)
    llm._append_usage(response)
    lines = llm.usage_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row == {
        "prompt_tokens": 19,
        "completion_tokens": 13,
        "cache_hit_tokens": 12,
        "cache_write_tokens": 4,
    }


def test_parse_stream_tool_call_deltas():
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "bash", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"cmd": "ls"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}},
    ]
    response = parse_stream_chunks(chunks)
    assert response.stop_reason == "tool_use"
    assert len(response.tool_calls) == 1
    fn = response.tool_calls[0]["function"]
    assert fn["name"] == "bash"
    assert fn["arguments"] == '{"cmd": "ls"}'
    assert response.prompt_tokens == 40


def test_parse_stream_reasoning_only_is_empty_end_turn():
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "long think"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120}},
    ]
    response = parse_stream_chunks(chunks)
    assert response.text == ""
    assert response.tool_calls == []
    assert response.stop_reason == "end_turn"
    assert response.finish_reason == "stop"
    assert response.completion_tokens == 40


def test_stream_waits_120s_for_first_chunk_then_allows_10_minutes():
    import asyncio
    import time

    from agent_loop.llm.client import _iter_sse_json

    class Content:
        def __init__(self, steps):
            self.steps = list(steps)

        async def readany(self):
            if not self.steps:
                return b""
            delay, data = self.steps.pop(0)
            if delay:
                await asyncio.sleep(delay)
            return data

    class Resp:
        def __init__(self, steps):
            self.content = Content(steps)
            self.connection = None

    async def collect(resp, open_timeout, stream_timeout):
        loop = asyncio.get_running_loop()
        out = []
        async for obj in _iter_sse_json(
            resp,
            open_deadline=loop.time() + open_timeout,
            stream_timeout=stream_timeout,
        ):
            out.append(obj)
        return out

    line = b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
    done = b"data: [DONE]\n\n"

    async def no_first_chunk():
        started = time.perf_counter()
        try:
            await collect(Resp([(1.0, line)]), 0.15, 5)
        except TimeoutError as exc:
            assert "等待流式输出" in str(exc)
            assert time.perf_counter() - started < 0.6
            return
        raise AssertionError("expected timeout before the first chunk")

    async def gap_after_open_is_allowed():
        # 第一块立刻到。中间停顿长过「等第一块」的时限，但短过整段时限，应读完。
        out = await collect(Resp([(0, line), (0.35, done)]), 0.15, 1)
        assert out == [{"choices": [{"delta": {"content": "a"}}]}]

    async def open_stream_still_ends_at_stream_timeout():
        started = time.perf_counter()
        try:
            await collect(Resp([(0, line), (1.0, done)]), 2, 0.2)
        except TimeoutError as exc:
            assert "流式输出超过时限" in str(exc)
            assert time.perf_counter() - started < 0.7
            return
        raise AssertionError("expected timeout after the stream started")

    asyncio.run(no_first_chunk())
    asyncio.run(gap_after_open_is_allowed())
    asyncio.run(open_stream_still_ends_at_stream_timeout())


def test_first_stream_chunk_clears_socket_idle_timeout():
    import asyncio

    from agent_loop.llm.client import _iter_sse_json

    class Content:
        async def readany(self):
            if self.done:
                return b""
            self.done = True
            return b"data: [DONE]\n\n"

        done = False

    class Protocol:
        def __init__(self):
            self.read_timeout = 120
            self.rescheduled = 0

        def start_timeout(self):
            self.rescheduled += 1

    class Resp:
        def __init__(self):
            self.content = Content()
            self.connection = type("Conn", (), {"protocol": Protocol()})()

    async def run():
        resp = Resp()
        loop = asyncio.get_running_loop()
        async for _obj in _iter_sse_json(
            resp, open_deadline=loop.time() + 1, stream_timeout=1
        ):
            pass
        assert resp.connection.protocol.read_timeout is None
        assert resp.connection.protocol.rescheduled == 1

    asyncio.run(run())


def test_parse_chat_response_keeps_usage():
    data = {
        "choices": [{"message": {"content": "hi", "tool_calls": []}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    response = parse_chat_response(data)
    assert response.text == "hi"
    assert response.prompt_tokens == 3
