"""拼 SSE 增量和 usage。"""
from agent_loop.llm.deepseek import _emit_text_delta, parse_chat_response, parse_stream_chunks


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
