"""browser-use 插件：开关、MCP stdio、schema、截图落盘。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from agent_loop.plugins.config import plugin_enabled
from agent_loop.plugins.host import PluginHost, _mcp_result_to_output
from agent_loop.plugins.mcp_stdio import McpStdioClient
from agent_loop.runtime.records import ToolOutput


def test_plugin_enabled_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    assert plugin_enabled("browser-use") is True
    (tmp_path / "config.json").write_text(
        json.dumps({"plugins": {"browser-use": False}}), encoding="utf-8"
    )
    assert plugin_enabled("browser-use") is False


def test_clip_exec_text_spills_huge_tab_list(tmp_path):
    from agent_loop.plugins.host import _clip_exec_text

    huge = str([{"url": f"https://example.com/{i}", "title": "t" * 80} for i in range(80)])
    assert len(huge.encode("utf-8")) > 8 * 1024
    shown = _clip_exec_text(huge, session_dir=str(tmp_path))
    assert len(shown.encode("utf-8")) < len(huge.encode("utf-8"))
    assert "truncated" in shown
    assert "full output:" in shown
    assert shown.startswith(huge[:100])


def test_mcp_result_image_not_base64(tmp_path):
    tiny = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X89QAAAAASUVORK5CYII="
    out = _mcp_result_to_output(
        {
            "content": [{"type": "image", "data": tiny, "mimeType": "image/png"}],
            "isError": False,
        },
        session_dir=str(tmp_path),
    )
    assert isinstance(out, ToolOutput)
    assert "base64" not in out.content
    path = Path(out.media["path"])
    assert path.is_file()
    assert "tools_result/browser" in str(path)


async def _run_fake_mcp(tmp_path: Path) -> None:
    fake = Path(__file__).resolve().parent / "fake_mcp_server.py"
    client = McpStdioClient([sys.executable, str(fake)])
    await client.start()
    try:
        listed = await client.request("tools/list", {})
        names = [t["name"] for t in listed["tools"]]
        assert names == ["browser_exec", "browser_screenshot"]
        result = await client.request(
            "tools/call",
            {"name": "browser_exec", "arguments": {"code": "print(1)"}},
        )
        text = result["content"][0]["text"]
        assert "print(1)" in text
    finally:
        await client.close()


def test_mcp_stdio_fake_server(tmp_path):
    asyncio.run(_run_fake_mcp(tmp_path))


def test_mcp_stdio_reads_tool_list_over_64k(monkeypatch):
    monkeypatch.setenv("FAKE_MCP_PROFILE", "computer")
    monkeypatch.setenv("MCP_BIG_LINE", "1")

    async def _run() -> None:
        fake = Path(__file__).resolve().parent / "fake_mcp_server.py"
        client = McpStdioClient([sys.executable, str(fake)])
        await client.start()
        try:
            listed = await client.request("tools/list", {})
            desc = listed["tools"][0]["description"]
            assert len(desc) > 64 * 1024
        finally:
            await client.close()

    asyncio.run(_run())


async def _run_host(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    fake = Path(__file__).resolve().parent / "fake_mcp_server.py"
    host = PluginHost()

    def _cmd():
        return [sys.executable, str(fake)]

    monkeypatch.setattr("agent_loop.plugins.host.browser_use_mcp_command", _cmd)
    await host.ensure_started()
    try:
        names = [s["function"]["name"] for s in host.openai_schemas()]
        assert names == ["browser_exec", "browser_screenshot"]
        out = await host.call_tool("browser_exec", {"code": "page_info()"})
        assert "page_info()" in str(out)
        shot = await host.call_tool("browser_screenshot", {}, session_dir=str(tmp_path))
        assert isinstance(shot, ToolOutput)
        assert Path(shot.media["path"]).is_file()
    finally:
        await host.stop()


def test_plugin_host_with_fake_mcp(tmp_path, monkeypatch):
    asyncio.run(_run_host(tmp_path, monkeypatch))


def test_checkbox_retry_does_not_open_another_inspect_page(monkeypatch):
    from agent_loop.plugins.host import enable_remote_debugging_checkbox

    scripts = []

    class _Done:
        returncode = 0
        stdout = "not-found\n"
        stderr = ""

    def _run(argv, input=None, **kwargs):
        scripts.append(input or "")
        return _Done()

    monkeypatch.setattr("agent_loop.plugins.host.subprocess.run", _run)
    assert enable_remote_debugging_checkbox() is False
    assert scripts
    assert all("open location" not in script for script in scripts)
    assert all("AXEnhancedUserInterface" in script for script in scripts)
    assert all("AXCheckBox" in script for script in scripts)


def test_needs_remote_debugging_setup():
    from agent_loop.plugins.host import _needs_remote_debugging_setup

    assert _needs_remote_debugging_setup(
        "RuntimeError: daemon default didn't come up -- check /tmp/bu-default.log"
    )
    assert _needs_remote_debugging_setup(
        "remote debugging is turned off for this browser instance"
    )
    assert _needs_remote_debugging_setup(
        'Chrome is asking "Allow remote debugging?" — click Allow'
    )
    assert not _needs_remote_debugging_setup("out:print(page_info())")


async def _run_host_waits_for_inspect(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    fail_flag = tmp_path / "fail_once"
    monkeypatch.setenv("MCP_FAIL_ONCE", str(fail_flag))
    fake = Path(__file__).resolve().parent / "fake_mcp_server.py"
    host = PluginHost()
    opened = {"n": 0}

    def _cmd():
        return [sys.executable, str(fake)]

    def _open():
        opened["n"] += 1
        return True

    monkeypatch.setattr("agent_loop.plugins.host.browser_use_mcp_command", _cmd)
    monkeypatch.setattr("agent_loop.plugins.host.open_chrome_inspect", _open)
    monkeypatch.setattr("agent_loop.plugins.host.enable_remote_debugging_checkbox", lambda: True)
    monkeypatch.setattr("agent_loop.plugins.host._chrome_remote_debugging_toggled", lambda: True)
    monkeypatch.setattr("agent_loop.plugins.host._CALL_TIMEOUT", 8)
    await host.ensure_started()
    try:
        out = await host.call_tool("browser_exec", {"code": "page_info()"})
        assert "page_info()" in str(out)
        assert opened["n"] == 1
    finally:
        await host.stop()


def test_plugin_host_opens_inspect_then_retries(tmp_path, monkeypatch):
    asyncio.run(_run_host_waits_for_inspect(tmp_path, monkeypatch))


async def _run_dead_mcp_fails_fast(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    fake = Path(__file__).resolve().parent / "fake_mcp_server.py"
    host = PluginHost()
    monkeypatch.setattr(
        "agent_loop.plugins.host.browser_use_mcp_command",
        lambda: [sys.executable, str(fake)],
    )
    await host.ensure_started()
    try:
        reader = host._client._reader_task
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):
            pass
        t0 = time.monotonic()
        out = await host.call_tool("browser_exec", {"code": "1"})
        assert time.monotonic() - t0 < 5
        assert isinstance(out, str)
    finally:
        await host.stop()


def test_screenshot_defaults_to_smaller_image():
    from agent_loop.plugins.host import _shrink_screenshot

    assert _shrink_screenshot("browser_screenshot", {})["max_dim"] == 1280
    assert _shrink_screenshot("browser_screenshot", {"max_dim": 400})["max_dim"] == 400
    assert _shrink_screenshot("browser_exec", {}) == {}


async def _run_restarts_after_mcp_closed() -> None:
    from types import SimpleNamespace

    host = PluginHost()
    host._client = SimpleNamespace(alive=True)
    calls = {"n": 0}

    async def once(name, args, session_dir=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("MCP server closed")
        return "page ok"

    restarted = {"n": 0}

    async def restart():
        restarted["n"] += 1
        host._client = SimpleNamespace(alive=True)

    host._call_tool_once = once
    host._restart = restart
    out = await host.call_tool("browser_exec", {"code": "print(1)"})
    assert out == "page ok"
    assert restarted["n"] == 1


def test_restarts_after_mcp_closed():
    asyncio.run(_run_restarts_after_mcp_closed())


def test_dead_mcp_fails_fast(tmp_path, monkeypatch):
    asyncio.run(_run_dead_mcp_fails_fast(tmp_path, monkeypatch))


def test_list_windows_summary_keeps_app_pid_id_and_title():
    from agent_loop.plugins.host import _format_window_list

    payload = {
        "content": [{"type": "text", "text": "Found 5 window(s)."}],
        "structuredContent": {
            "windows": [
                {"app_name": "WindowManager", "pid": 1, "window_id": 2, "title": "菜单", "is_on_screen": True, "z_index": 9},
                {"app_name": "企业微信", "pid": 653, "window_id": 1, "title": "", "is_on_screen": True},
                {
                    "app_name": "企业微信",
                    "pid": 653,
                    "window_id": 13172,
                    "title": "企业微信",
                    "is_on_screen": True,
                    "z_index": 3,
                },
                {
                    "app_name": "企业微信",
                    "pid": 653,
                    "window_id": 99,
                    "title": "后面的窗口",
                    "is_on_screen": True,
                    "z_index": 1,
                },
                {"app_name": "飞书", "pid": 9, "window_id": 8, "title": "飞书", "is_on_screen": False},
            ]
        },
    }
    text = _format_window_list(payload)
    assert text.splitlines() == [
        "1 window(s). One on-screen window per app. Pass pid for the rest.",
        "企业微信  pid=653  window_id=13172  企业微信",
    ]
    narrowed = _format_window_list(payload, narrowed=True)
    assert "window_id=99" in narrowed
    assert "window_id=8" in narrowed
    assert "WindowManager" not in narrowed


def test_computer_plugin_forwards_one_tool_and_writes_catalog(tmp_path, monkeypatch):
    asyncio.run(_run_computer(tmp_path, monkeypatch))


async def _run_computer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_MCP_PROFILE", "computer")
    fake = Path(__file__).resolve().parent / "fake_mcp_server.py"
    host = PluginHost()
    monkeypatch.setattr(
        "agent_loop.plugins.host.computer_mcp_command",
        lambda: [sys.executable, str(fake)],
    )
    await host.ensure_started()
    try:
        names = [item["function"]["name"] for item in host.openai_schemas()]
        assert names == ["computer"]
        catalog = tmp_path / "plugins" / "computer-use" / "tools.md"
        text = catalog.read_text(encoding="utf-8")
        assert "\n# click\n" in f"\n{text}"
        assert "element_index" in text
        assert "tools.md" in host.openai_schemas()[0]["function"]["description"]
        out = await host.call_tool(
            "computer",
            {
                "name": "click",
                "arguments": {"pid": 4, "window_id": 7, "element_index": 2},
            },
            session_dir=str(tmp_path),
        )
        assert "clicked 2" in str(out)
        shot = await host.call_tool(
            "computer",
            {"name": "get_window_state", "arguments": '{"pid": 4, "window_id": 7}'},
            session_dir=str(tmp_path),
        )
        assert isinstance(shot, ToolOutput)
        assert "tools_result/computer" in shot.media["path"]
        assert "base64" not in shot.content
        missing = await host.call_tool("computer", {"name": "nope"})
        assert "unknown" in str(missing)
    finally:
        await host.stop()


def test_permissions_pending_asks_the_system_dialog(monkeypatch):
    asyncio.run(_run_permissions_pending())


async def _run_permissions_pending() -> None:
    from types import SimpleNamespace

    host = PluginHost()
    host._computer = SimpleNamespace(alive=True)
    host._computer_names = {"list_apps"}
    calls = {"n": 0}

    async def once(name, args, session_dir=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                "permissions_pending: macOS Accessibility or Screen Recording "
                "permission is still pending"
            )
        return "apps ok"

    granted = {"n": 0}

    async def grant():
        granted["n"] += 1

    host._call_computer_once = once
    host._prompt_cua_permissions = grant
    out = await host._call_computer({"name": "list_apps"}, None)
    assert out == "apps ok"
    assert granted["n"] == 1
    assert calls["n"] == 2


def test_computer_plugin_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"plugins": {"computer-use": False}}), encoding="utf-8"
    )
    fake = Path(__file__).resolve().parent / "fake_mcp_server.py"
    host = PluginHost()
    monkeypatch.setattr(
        "agent_loop.plugins.host.computer_mcp_command",
        lambda: [sys.executable, str(fake)],
    )

    async def _run() -> None:
        await host.ensure_started()
        try:
            assert host.openai_schemas() == []
            assert not (tmp_path / "plugins" / "computer-use" / "tools.md").is_file()
        finally:
            await host.stop()

    asyncio.run(_run())


def test_plugin_host_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"plugins": {"browser-use": False}}), encoding="utf-8"
    )
    host = PluginHost()

    async def _run() -> None:
        await host.ensure_started()
        try:
            assert host.openai_schemas() == []
        finally:
            await host.stop()

    asyncio.run(_run())
