"""bash 失败：run_bash 要 throw；ToolRuntime 当场关单；下一轮 LLM 能看到错误。"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from agent_loop.loop import ReactAgentLoop
from agent_loop.llm import LLMResponse
from agent_loop.recover import inspect_log
from agent_loop.session_log import SessionLog, session_log_path
from agent_loop.runtime.tool_runtime import ToolRuntime
from agent_loop.tools.bash_tool import run_bash


class FakeSandbox:
    def __init__(self, stdout="", stderr="", exit_code=0, error=None, hang=0):
        self.commands = self
        self._stdout = stdout
        self._stderr = stderr
        self._exit_code = exit_code
        self._error = error
        self._hang = hang

    def run(self, cmd, timeout=120):
        if self._error:
            raise self._error
        if self._hang:
            time.sleep(self._hang)
        return SimpleNamespace(
            stdout=self._stdout,
            stderr=self._stderr,
            exit_code=self._exit_code,
        )


class FakePost:
    def __init__(self):
        self.send_from = "ReActAgent"
        self.send_to = ""
        self.message = ""


class FakeProxy:
    def __init__(self):
        self.post = FakePost()

    def update_send_to(self, role):
        self.post.send_to = role

    def end(self, msg=""):
        return self.post


class FakeEmitter:
    def create_post_proxy(self, send_from):
        proxy = FakeProxy()
        proxy.post.send_from = send_from
        return proxy


class FakeDeps:
    def __init__(self, sandbox=None):
        self._sandbox = sandbox

    async def ensure_sandbox_async(self):
        return self._sandbox


class FakeMemory:
    def __init__(self, query="hello"):
        self.conversation = SimpleNamespace(rounds=[SimpleNamespace(user_query=query)])


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            return LLMResponse(text="NO_REPLY")
        return self.responses.pop(0)


def _bash_call(cmd="false"):
    return {
        "id": "call-1",
        "type": "function",
        "function": {"name": "bash", "arguments": '{"cmd": "%s"}' % cmd},
    }


def _uad(tmp_path, **extra):
    data = {"sessionId": "s1", "workspace": str(tmp_path)}
    data.update(extra)
    return data


def test_run_bash_local_echo():
    out = asyncio.run(run_bash("echo hello"))
    assert "hello" in out


def test_run_bash_empty_cmd_raises():
    with pytest.raises(ValueError, match="cmd 为空"):
        asyncio.run(run_bash("  ", FakeSandbox()))


def test_run_bash_sandbox_exception_raises():
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run_bash("ls", FakeSandbox(error=RuntimeError("boom"))))


def test_run_bash_nonzero_exit_raises():
    with pytest.raises(RuntimeError, match="nope"):
        asyncio.run(run_bash("false", FakeSandbox(stderr="nope", exit_code=1)))


def test_run_bash_hang_times_out():
    with pytest.raises(RuntimeError, match="timed out after 0.2"):
        asyncio.run(run_bash("sleep 99", FakeSandbox(hang=2), timeout=0.2))


def test_run_bash_timeout_retries_once_then_succeeds():
    class Flaky:
        def __init__(self):
            self.commands = self
            self.n = 0

        def run(self, cmd, timeout=120):
            self.n += 1
            if self.n == 1:
                time.sleep(1)
            return SimpleNamespace(stdout="ok", stderr="", exit_code=0)

    sandbox = Flaky()
    out = asyncio.run(run_bash("echo ok", sandbox, timeout=0.2))
    assert out == "ok"
    assert sandbox.n == 2


def test_run_bash_zero_exit_returns_stdout():
    out = asyncio.run(run_bash("echo hi", FakeSandbox(stdout="hi\n")))
    assert out == "hi"


def test_runtime_nonzero_exit_closes_ticket_with_is_error():
    runtime = ToolRuntime()
    result = asyncio.run(
        runtime.call_tool(_bash_call("false"), FakeSandbox(stderr="nope", exit_code=1))
    )
    assert result.is_error is True
    assert "nope" in result.content
    assert len(runtime.tool_started_records) == 1
    assert result.result_id == runtime.tool_started_records[0].result_id


def test_runtime_execute_throw_does_not_leave_unfinished():
    runtime = ToolRuntime()
    result = asyncio.run(
        runtime.call_tool(_bash_call("ls"), FakeSandbox(error=RuntimeError("sandbox down")))
    )
    assert result.is_error is True
    assert "sandbox down" in result.content
    assert inspect_log([]).unfinished == []
    # 本轮内存里 started 和 result 成对，不是未关工单
    assert len(runtime.tool_started_records) == 1
    assert len(runtime.tool_result_records) == 1
    assert runtime.tool_result_records[0].result_id == runtime.tool_started_records[0].result_id


def test_loop_sees_error_and_continues(tmp_path):
    llm = ScriptedLLM(
        [
            LLMResponse(text="", tool_calls=[_bash_call("false")], stop_reason="tool_use"),
            LLMResponse(text="命令失败了我改口了", tool_calls=[], stop_reason="end_turn"),
        ]
    )
    loop = ReactAgentLoop(FakeDeps(), None, None)
    loop._llm = llm
    answer = asyncio.run(loop._run_loop("跑 false", _uad(tmp_path)))

    assert answer == "命令失败了我改口了"
    tool_messages = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert loop.tool_result_records[0].is_error is True
    assert "exited with code" in tool_messages[0]["content"] or tool_messages[0]["content"]

    log = SessionLog(session_log_path(_uad(tmp_path))).read_all()
    plan = inspect_log(log)
    assert plan.unfinished == []
    assert any(row.get("type") == "tool_started" for row in log)
    assert any(row.get("type") == "tool_result" and row.get("is_error") for row in log)
