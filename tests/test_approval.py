import asyncio
import json

from agent_loop.approval import bash_first_word, grant_key, needs_approval
from agent_loop.llm import LLMResponse
from agent_loop.loop import ReactAgentLoop
from agent_loop.recover import inspect_log
from agent_loop.runtime.tool_concurrency import run_tool_calls
from agent_loop.runtime.tool_runtime import USER_DENIED, ToolRuntime
from agent_loop.session_log import SessionLog, session_log_path
from agent_loop.tools import read_tool


def _call(call_id, name, arguments):
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class _ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            return LLMResponse(text="NO_REPLY")
        return self.responses.pop(0)


def test_safe_tools_skip_approval_and_risky_ones_do_not():
    assert needs_approval("read", {"path": "a.py"}) is False
    assert needs_approval("browser_screenshot", {}) is False
    assert needs_approval("computer", {"name": "list_windows"}) is False
    assert needs_approval("computer", {"name": "click", "arguments": {"pid": 1}}) is True
    assert needs_approval("bash", {"cmd": "ls"}) is True
    assert needs_approval("write", {"path": "a.py"}) is True


def test_grant_keys_match_the_same_file_command_or_app():
    assert grant_key("write", {"path": "a.py"}) == grant_key("edit", {"path": "a.py"})
    assert grant_key("write", {"path": "a.py"}) != grant_key("write", {"path": "b.py"})
    assert grant_key("bash", {"cmd": "git status"}) == "bash:git"
    assert grant_key("bash", {"cmd": "git push"}) == "bash:git"
    assert grant_key("bash", {"cmd": "rm -rf x"}) is None
    assert grant_key("bash", {"cmd": "sudo ls"}) is None
    assert grant_key("bash", {"cmd": "curl https://example.com"}) is None
    click = {"name": "click", "arguments": {"pid": 653, "x": 1, "y": 2}}
    assert grant_key("computer", click) == "computer:click:pid:653"
    assert grant_key("computer", {"name": "type_text", "arguments": {"pid": 653}}) != grant_key(
        "computer", click
    )
    assert grant_key("computer", {"name": "click", "arguments": {"pid": 1}}) != grant_key(
        "computer", click
    )
    assert grant_key("browser_exec", {"code": "click()"}) is None
    assert bash_first_word("FOO=1 git status") == "git"


def test_denied_tool_is_a_result_and_does_not_start():
    async def _run() -> None:
        runtime = ToolRuntime()

        async def deny(name, args):
            return "deny"

        runtime.approver = deny
        result = await runtime.call_tool(
            {"id": "c1", "function": {"name": "bash", "arguments": '{"cmd": "echo hi"}'}}
        )
        assert result.is_error
        assert result.content == USER_DENIED
        assert result.terminate is True
        assert runtime.tool_started_records == []

    asyncio.run(_run())


def test_deny_does_not_run_the_rest_of_the_batch(tmp_path):
    """拒绝一次之后，同批里还没跑的命令不再执行，后面的也不再询问。"""

    async def _run() -> None:
        executed = []
        original = read_tool.execute

        async def wrapped(args, sandbox=None, workspace=None, session_dir=None):
            executed.append(args.get("path"))
            return await original(args, sandbox, workspace, session_dir)

        read_tool.execute = wrapped
        asked = []

        async def deny(name, args):
            if not needs_approval(name, args):
                return "allow"
            asked.append((name, args.get("cmd")))
            return "deny"

        try:
            runtime = ToolRuntime()
            runtime.workspace = str(tmp_path)
            runtime.approver = deny
            target = tmp_path / "note.txt"
            target.write_text("hello", encoding="utf-8")
            results = await run_tool_calls(
                runtime,
                [
                    _call("read-1", "read", {"path": str(target)}),
                    _call("bash-1", "bash", {"cmd": "echo hi"}),
                    _call("bash-2", "bash", {"cmd": "echo again"}),
                ],
            )
        finally:
            read_tool.execute = original

        assert executed == []
        assert asked == [("bash", "echo hi")]
        assert [item.content for item in results] == [USER_DENIED, USER_DENIED, USER_DENIED]
        assert results[1].terminate is True
        assert results[0].terminate is False
        assert inspect_log([]).unfinished == []
        assert len(runtime.tool_started_records) == 1
        assert results[0].result_id == runtime.tool_started_records[0].result_id

    asyncio.run(_run())


def test_deny_stops_the_turn_instead_of_asking_the_model_again(tmp_path):
    async def deny(name, args):
        return "deny"

    llm = _ScriptedLLM(
        [
            LLMResponse(
                text="",
                tool_calls=[_call("bash-1", "bash", {"cmd": "echo hi"})],
                stop_reason="tool_use",
            ),
        ]
    )
    loop = ReactAgentLoop(None, None, None)
    loop._llm = llm
    loop.runtime.approver = deny
    answer = asyncio.run(
        loop._run_loop(
            "查询股价",
            {"sessionId": "s1", "workspace": str(tmp_path), "install_sigint": False},
        )
    )

    assert answer == "Stopped."
    assert sum(1 for call in llm.calls if call["tools"]) == 1
    assert loop.tool_started_records == []
    rows = SessionLog(session_log_path({"sessionId": "s1", "workspace": str(tmp_path)})).read_all()
    assert inspect_log(rows).action == "idle"
    assert any(
        row.get("type") == "tool_result" and row.get("content") == USER_DENIED for row in rows
    )
