"""拼 SSE 增量和 usage。"""
from agent_loop.llm.deepseek import (
    DeepSeekLLM,
    _emit_text_delta,
    canonical_model,
    parse_chat_response,
    parse_stream_chunks,
    save_model,
)
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
    llm = DeepSeekLLM(api_key="sk-test", model="deepseek-v4-flash")
    assert llm.model == "deepseek-flash"
    assert llm.supports_images is True
    assert llm.use("deepseek-v4-pro") == "deepseek-v4-pro"
    assert llm.supports_images is False
    assert llm.context_window == 1_000_000


def test_glm_switches_endpoint_and_keeps_thinking_on(monkeypatch):
    import asyncio

    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
    llm = DeepSeekLLM(api_key="sk-test")
    assert llm.use("glm-5.3-flash") == "glm-5.3-flash"
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
    assert llm.use("deepseek-flash") == "deepseek-flash"
    assert llm.api_key == "ds-test"
    assert llm.api_base == "https://api.deepseek.com"


def test_zhipu_balance_429_is_not_retried():
    from agent_loop.llm.deepseek import LLMHTTPError, is_retryable_llm_error

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


def test_parse_chat_response_keeps_usage():
    data = {
        "choices": [{"message": {"content": "hi", "tool_calls": []}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    response = parse_chat_response(data)
    assert response.text == "hi"
    assert response.prompt_tokens == 3
