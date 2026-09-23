import asyncio

from agent_loop.approval import bash_first_word, grant_key, needs_approval
from agent_loop.runtime.tool_runtime import ToolRuntime


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
        assert result.content == "The user denied this action."
        assert runtime.tool_started_records == []

    asyncio.run(_run())
