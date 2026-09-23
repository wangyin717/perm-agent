"""bash 失败：run_bash 要 throw；ToolRuntime 当场关单；下一轮 LLM 能看到错误。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_loop.loop import ReactAgentLoop
from agent_loop.llm import LLMResponse
from agent_loop.recover import inspect_log
from agent_loop.session_log import SessionLog, session_log_path
from agent_loop.runtime.tool_runtime import ToolRuntime
from agent_loop.tools.bash_tool import MAX_BYTES, MAX_LINES, run_bash


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


def test_bash_blocks_tk_in_command():
    import json

    runtime = ToolRuntime()
    result = asyncio.run(
        runtime.call_tool(
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps(
                        {"cmd": "python3 -c 'import tkinter as tk; tk.Tk()'"}
                    ),
                },
            },
            FakeSandbox(stdout="should not run"),
        )
    )
    assert result.is_error is True
    assert "Abort trap" in result.content or "GUI" in result.content
    assert "should not run" not in result.content


def test_bash_blocks_python_script_that_opens_tk(tmp_path):
    script = tmp_path / "game.py"
    script.write_text("import tkinter as tk\nroot = tk.Tk()\n", encoding="utf-8")
    runtime = ToolRuntime()
    result = asyncio.run(
        runtime.call_tool(
            _bash_call(f"python3 {script}"),
            FakeSandbox(stdout="should not run"),
        )
    )
    assert result.is_error is True
    assert "GUI" in result.content or "tk.Tk" in result.content


def test_bash_allows_headless_python_test(tmp_path):
    script = tmp_path / "test_logic.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    result = asyncio.run(
        runtime.call_tool(
            _bash_call(f"python3 {script}"),
            FakeSandbox(stdout="ok\n"),
        )
    )
    assert result.is_error is False
    assert result.content.strip() == "ok"


def test_run_bash_zero_exit_returns_stdout():
    out = asyncio.run(run_bash("echo hi", FakeSandbox(stdout="hi\n")))
    assert out == "hi"


def test_short_output_does_not_spill(tmp_path):
    session = tmp_path / "chat"
    out = asyncio.run(
        run_bash(
            "echo hi",
            FakeSandbox(stdout="hi\n"),
            session_dir=str(session),
            workspace=str(tmp_path),
        )
    )
    assert out == "hi"
    bash_dir = session / "workspace" / "tools_result" / "bash"
    assert not bash_dir.exists() or not list(bash_dir.glob("*.txt"))


def test_long_lines_spill_head_tail(tmp_path):
    lines = [f"L{i:04d}" for i in range(2500)]
    body = "\n".join(lines)
    session = tmp_path / "chat"
    out = asyncio.run(
        run_bash(
            "seq",
            FakeSandbox(stdout=body + "\n"),
            session_dir=str(session),
            workspace=str(tmp_path),
        )
    )
    assert "L0000" in out
    assert "L0199" in out
    assert "L0200" not in out
    assert "... 500 lines omitted ..." in out
    assert "L0700" in out
    assert "L2499" in out
    assert "saved:" in out
    assert "Do not pipe head/tail" in out
    saved = [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("saved:")][0]
    path = Path(saved) if Path(saved).is_absolute() else tmp_path / saved
    stored = path.read_text(encoding="utf-8")
    assert stored.splitlines()[0] == "L0000"
    assert stored.splitlines()[-1] == "L2499"
    assert len(stored.splitlines()) == 2500


def test_over_bytes_spills(tmp_path):
    line = "x" * 200
    body = "\n".join([line] * 400)
    assert len(body.encode("utf-8")) > MAX_BYTES
    assert body.count("\n") + 1 < MAX_LINES
    session = tmp_path / "chat"
    out = asyncio.run(
        run_bash(
            "big",
            FakeSandbox(stdout=body),
            session_dir=str(session),
            workspace=str(tmp_path),
        )
    )
    assert "saved:" in out
    assert len(out.encode("utf-8")) <= MAX_BYTES + 800


def test_long_failure_spills_then_raises(tmp_path):
    lines = [f"E{i:04d}" for i in range(2100)]
    body = "\n".join(lines)
    session = tmp_path / "chat"
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(
            run_bash(
                "false",
                FakeSandbox(stderr=body + "\n", exit_code=1),
                session_dir=str(session),
                workspace=str(tmp_path),
            )
        )
    msg = str(exc.value)
    assert "E0000" in msg
    assert "E2099" in msg
    assert "saved:" in msg
    saved = [ln.split(" ", 1)[1] for ln in msg.splitlines() if ln.startswith("saved:")][0]
    path = Path(saved) if Path(saved).is_absolute() else tmp_path / saved
    assert "E2099" in path.read_text(encoding="utf-8")


def test_long_without_session_notes_no_file():
    body = "\n".join(f"L{i}" for i in range(2100))
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(run_bash("false", FakeSandbox(stdout=body, exit_code=1)))
    msg = str(exc.value)
    assert "no session directory" in msg
    assert "saved:" not in msg
    assert "... " in msg and "omitted" in msg


def test_runtime_long_success_has_no_8k_cut(tmp_path):
    body = "\n".join(f"L{i:04d}" for i in range(2100))
    runtime = ToolRuntime()
    runtime.workspace = str(tmp_path)
    runtime.session_dir = str(tmp_path / "chat")
    result = asyncio.run(
        runtime.call_tool(
            _bash_call("seq"),
            FakeSandbox(stdout=body + "\n"),
        )
    )
    assert result.is_error is False
    assert "...[output truncated]" not in result.content
    assert "saved:" in result.content
    assert "L2099" in result.content


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


def test_tool_step_emits_the_full_sentence(tmp_path, monkeypatch):
    from agent_loop import events

    monkeypatch.setattr(events, "print_to_stdout", False)
    seen = []
    unsub = events.subscribe(lambda event: seen.append(event))
    sentence = "我先查一下微软当前的市值，然后生成 Word 文档。"
    try:
        llm = ScriptedLLM(
            [
                LLMResponse(
                    text=sentence,
                    tool_calls=[_bash_call("echo ok")],
                    stop_reason="tool_use",
                ),
                LLMResponse(text="好了", tool_calls=[], stop_reason="end_turn"),
            ]
        )
        loop = ReactAgentLoop(FakeDeps(), None, None)
        loop._llm = llm
        answer = asyncio.run(loop._run_loop("查市值", _uad(tmp_path)))
    finally:
        unsub()
    assert answer == "好了"
    kinds = [event.get("kind") for event in seen]
    assistant_at = kinds.index("assistant")
    tools_at = kinds.index("tools")
    assert assistant_at < tools_at
    assert seen[assistant_at]["text"] == sentence


def test_empty_end_turn_retries_then_answers(tmp_path, monkeypatch):
    from agent_loop import events

    monkeypatch.setattr(events, "print_to_stdout", False)
    llm = ScriptedLLM(
        [
            LLMResponse(text="", tool_calls=[], stop_reason="end_turn"),
            LLMResponse(text="补上的回复", tool_calls=[], stop_reason="end_turn"),
        ]
    )
    loop = ReactAgentLoop(FakeDeps(), None, None)
    loop._llm = llm
    answer = asyncio.run(loop._run_loop("分析市场", _uad(tmp_path)))
    assert answer == "补上的回复"
    assert len(llm.calls) >= 2


def test_empty_end_turn_twice_emits_error(tmp_path, monkeypatch):
    from agent_loop import events

    monkeypatch.setattr(events, "print_to_stdout", False)
    seen = []
    unsub = events.subscribe(lambda e: seen.append(e))
    try:
        llm = ScriptedLLM(
            [
                LLMResponse(text="", tool_calls=[], stop_reason="end_turn", finish_reason="stop"),
                LLMResponse(text="", tool_calls=[], stop_reason="end_turn", finish_reason="stop"),
            ]
        )
        loop = ReactAgentLoop(FakeDeps(), None, None)
        loop._llm = llm
        answer = asyncio.run(loop._run_loop("分析市场", _uad(tmp_path)))
    finally:
        unsub()
    assert answer == ""
    errors = [e for e in seen if e.get("kind") == "error"]
    assert errors, seen
    assert "model returned no text" in str(errors[0].get("text") or "")
    log = SessionLog(session_log_path(_uad(tmp_path))).read_all()
    assistants = [r for r in log if r.get("type") == "assistant"]
    assert assistants
    assert (assistants[-1].get("content") or "") == ""
