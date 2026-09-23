"""会话级插件宿主：起 MCP、给出 schema、转发 tool call。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent_loop import events
from agent_loop.paths import spark_home
from agent_loop.plugins.config import plugin_enabled
from agent_loop.plugins.mcp_stdio import McpStdioClient
from agent_loop.runtime.records import ToolOutput

BROWSER_EXEC = "browser_exec"
BROWSER_SCREENSHOT = "browser_screenshot"
COMPUTER = "computer"
# 等用户点 Allow 的总时限。单次 RPC 必须短，否则 MCP 挂了 TUI 会假死。
_CALL_TIMEOUT = 6000.0
_RPC_TIMEOUT = 120.0
_ALLOW_POLL_TIMEOUT = 20.0
_EXEC_MAX_BYTES = 8 * 1024
_EXEC_HEAD_CHARS = 3000


class _PluginTool:
    REPLAY = "never"
    READ_ONLY = False
    BEFORE_HOOKS: list = []
    AFTER_HOOKS: list = []

    def __init__(self, name: str, host: "PluginHost"):
        self.NAME = name
        self._host = host

    async def execute(self, args, sandbox=None, workspace=None, session_dir=None):
        return await self._host.call_tool(self.NAME, args or {}, session_dir=session_dir)


class PluginHost:
    def __init__(self) -> None:
        self._client: Optional[McpStdioClient] = None
        self._computer: Optional[McpStdioClient] = None
        self._schemas: List[Dict[str, Any]] = []
        self._tools: Dict[str, _PluginTool] = {}
        self._browser_schemas: List[Dict[str, Any]] = []
        self._browser_tools: Dict[str, _PluginTool] = {}
        self._computer_schemas: List[Dict[str, Any]] = []
        self._computer_tools: Dict[str, _PluginTool] = {}
        self._computer_names: set[str] = set()
        self._started = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._log_file = None
        self._computer_log = None
        self._session_dir: Optional[Path] = None
        self._abort = None

    def openai_schemas(self) -> List[Dict[str, Any]]:
        return list(self._schemas)

    def get_tool(self, name: str) -> Optional[_PluginTool]:
        return self._tools.get(name)

    def tool_names(self) -> List[str]:
        return list(self._tools)

    def bind_abort(self, abort) -> None:
        self._abort = abort

    def cancel(self) -> None:
        abort = self._abort
        if abort is not None:
            abort.abort()

    def _aborted(self) -> bool:
        abort = self._abort
        return bool(abort is not None and abort.aborted)

    async def _sleep_or_abort(self, seconds: float) -> None:
        abort = self._abort
        if abort is None:
            await asyncio.sleep(seconds)
            return
        if abort.aborted:
            raise RuntimeError("aborted")
        try:
            await asyncio.wait_for(abort.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        raise RuntimeError("aborted")

    def set_session_dir(self, session_dir: Path) -> None:
        self._session_dir = Path(session_dir)
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = _reopen_log(self._log_file, self._session_dir / "browser-use-mcp.log")
        self._computer_log = _reopen_log(
            self._computer_log, self._session_dir / "computer-use-mcp.log"
        )
        if self._client is not None:
            self._client.set_stderr_file(self._log_file)
        if self._computer is not None:
            self._computer.set_stderr_file(self._computer_log)

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._started.is_set():
                return
            try:
                await self._start_unlocked()
            finally:
                self._started.set()

    async def stop(self) -> None:
        browser = self._client
        computer = self._computer
        self._client = None
        self._computer = None
        self._schemas = []
        self._tools = {}
        self._browser_schemas = []
        self._browser_tools = {}
        self._computer_schemas = []
        self._computer_tools = {}
        self._computer_names = set()
        for client in (browser, computer):
            if client is not None:
                await client.close()
        for handle in (self._log_file, self._computer_log):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        self._log_file = None
        self._computer_log = None

    def stop_sync(self) -> None:
        for client in (self._client, self._computer):
            if client is not None and client._proc is not None:
                try:
                    client._proc.terminate()
                except Exception:
                    pass
        self._client = None
        self._computer = None
        for handle in (self._log_file, self._computer_log):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        self._log_file = None
        self._computer_log = None

    async def call_tool(self, name: str, args: Dict[str, Any], session_dir: Optional[str] = None) -> Any:
        if name == COMPUTER:
            return await self._call_computer(args or {}, session_dir)
        args = _shrink_screenshot(name, args)
        if self._client is None or not self._client.alive:
            await self._restart()
        try:
            return await self._call_tool_once(name, args, session_dir)
        except RuntimeError as exc:
            if not _mcp_died(exc):
                raise
            logging.warning("[plugins] browser-use MCP died (%s); restarting", exc)
            await self._restart()
            return await self._call_tool_once(name, args, session_dir)

    async def _restart(self) -> None:
        async with self._start_lock:
            old = self._client
            self._client = None
            if old is not None:
                await old.close()
            await self._start_browser()

    async def _call_tool_once(self, name: str, args: Dict[str, Any], session_dir: Optional[str] = None) -> Any:
        if self._client is None or not self._client.alive:
            raise RuntimeError("browser-use plugin is not running")
        out, text = await self._invoke(name, args, session_dir)
        if "mcp server closed" in (text or "").lower():
            raise RuntimeError(text)
        if name not in (BROWSER_EXEC, BROWSER_SCREENSHOT) or not _needs_remote_debugging_setup(text):
            return out
        if _needs_inspect_page(text):
            opened = open_chrome_inspect()
            ticked = enable_remote_debugging_checkbox()
            logging.info(
                "[plugins] chrome inspect opened=%s checkbox=%s; waiting for Allow",
                opened,
                ticked,
            )
            if ticked:
                notice = (
                    "已勾选检查页上的「Allow remote debugging for this browser instance」。"
                    "请在弹出的 **Allow remote debugging?** 里点 Allow。"
                )
            else:
                notice = (
                    "没能勾上检查页的复选框。"
                    "请在已打开的检查页勾选一次 "
                    "「Allow remote debugging for this browser instance」，"
                    "再在 **Allow remote debugging?** 里点 Allow。"
                )
            events.emit("notice", text=notice)
        else:
            events.emit(
                "notice",
                text="Chrome 在等你确认远程调试。请点弹出的 **Allow remote debugging?**。",
            )
        # 不要每 1.5 秒再调 browser_exec：harness 会再 open location，开出一堆检查页。
        # 勾选写进 Local State 之后，只再连一次，等 Allow 弹窗。
        deadline = time.monotonic() + _CALL_TIMEOUT
        last = out
        while time.monotonic() < deadline:
            await self._sleep_or_abort(1.0)
            if not _chrome_remote_debugging_toggled():
                continue
            if self._client is None or not self._client.alive:
                raise RuntimeError("browser-use plugin is not running")
            last, text = await self._invoke(name, args, session_dir, timeout=_CALL_TIMEOUT)
            return last
        return last

    async def _invoke(
        self, name: str, args: Dict[str, Any], session_dir: Optional[str],
        timeout: float = _RPC_TIMEOUT,
    ) -> Tuple[Any, str]:
        if self._aborted():
            raise RuntimeError("aborted")
        client = self._client
        if client is None:
            raise RuntimeError("browser-use plugin is not running")
        request = asyncio.create_task(
            client.request(
                "tools/call",
                {"name": name, "arguments": args},
                timeout=timeout,
            )
        )
        abort = self._abort
        if abort is None:
            result = await request
        else:
            waiter = asyncio.create_task(abort.wait())
            done, pending = await asyncio.wait(
                {request, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if abort.aborted:
                raise RuntimeError("aborted")
            result = await request
        try:
            out = _mcp_result_to_output(result, session_dir=session_dir)
        except RuntimeError as exc:
            return str(exc), str(exc)
        text = out.content if isinstance(out, ToolOutput) else str(out)
        clipped = _clip_exec_text(text, session_dir)
        if isinstance(out, ToolOutput):
            out.content = clipped
            return out, clipped
        return clipped, clipped

    async def _start_unlocked(self) -> None:
        if self._session_dir is not None:
            self.set_session_dir(self._session_dir)
        await self._start_browser()
        await self._start_computer()

    async def _start_browser(self) -> None:
        if not plugin_enabled("browser-use", default=True):
            self._browser_schemas = []
            self._browser_tools = {}
            self._publish()
            return
        cmd = browser_use_mcp_command()
        if not cmd:
            logging.warning("[plugins] browser-use CLI not found; plugin skipped")
            self._browser_schemas = []
            self._browser_tools = {}
            self._publish()
            return
        env = _plugin_env()
        env.setdefault("BH_CLIENT", "permanent")
        logging.info("[plugins] starting browser-use MCP: %s", " ".join(cmd))
        client = McpStdioClient(cmd, env=env)
        client.set_stderr_file(self._log_file)
        try:
            await client.start()
            listed = await client.request("tools/list", {})
        except Exception:
            logging.warning("[plugins] browser-use MCP failed to start", exc_info=True)
            await client.close()
            self._browser_schemas = []
            self._browser_tools = {}
            self._publish()
            return
        self._client = client
        tools = (listed or {}).get("tools") or []
        schemas = []
        adapters = {}
        for item in tools:
            name = str(item.get("name") or "")
            if name not in (BROWSER_EXEC, BROWSER_SCREENSHOT):
                continue
            schemas.append(_openai_schema(item))
            adapters[name] = _PluginTool(name, self)
        self._browser_schemas = schemas
        self._browser_tools = adapters
        self._publish()
        logging.info("[plugins] browser-use MCP up tools=%s", list(adapters))

    async def _start_computer(self) -> None:
        if not plugin_enabled("computer-use", default=True):
            self._computer_schemas = []
            self._computer_tools = {}
            self._computer_names = set()
            self._publish()
            return
        cmd = computer_mcp_command()
        if not cmd:
            logging.warning("[plugins] cua-driver not found; computer-use skipped")
            self._computer_schemas = []
            self._computer_tools = {}
            self._computer_names = set()
            self._publish()
            return
        logging.info("[plugins] starting cua-driver MCP: %s", " ".join(cmd))
        client = McpStdioClient(cmd, env=_plugin_env())
        client.set_stderr_file(self._computer_log)
        try:
            await client.start()
            listed = await client.request("tools/list", {}, timeout=_RPC_TIMEOUT)
        except Exception:
            logging.warning("[plugins] cua-driver MCP failed to start", exc_info=True)
            await client.close()
            self._computer_schemas = []
            self._computer_tools = {}
            self._computer_names = set()
            self._publish()
            return
        tools = [item for item in ((listed or {}).get("tools") or []) if isinstance(item, dict)]
        names = {str(item.get("name") or "") for item in tools if item.get("name")}
        if not names:
            logging.warning("[plugins] cua-driver MCP listed no tools")
            await client.close()
            self._computer_schemas = []
            self._computer_tools = {}
            self._computer_names = set()
            self._publish()
            return
        catalog = _write_computer_catalog(tools)
        self._computer = client
        self._computer_names = names
        self._computer_schemas = [_computer_schema(catalog)]
        self._computer_tools = {COMPUTER: _PluginTool(COMPUTER, self)}
        self._publish()
        logging.info("[plugins] cua-driver MCP up tools=%s catalog=%s", len(names), catalog)

    def _publish(self) -> None:
        self._schemas = list(self._browser_schemas) + list(self._computer_schemas)
        self._tools = {**self._browser_tools, **self._computer_tools}

    async def _restart_computer(self) -> None:
        async with self._start_lock:
            old = self._computer
            self._computer = None
            if old is not None:
                await old.close()
            await self._start_computer()

    async def _call_computer(self, args: Dict[str, Any], session_dir: Optional[str]) -> Any:
        tool_name, tool_args, error = _computer_call_args(args)
        if error:
            return error
        if self._computer is None or not self._computer.alive:
            await self._restart_computer()
        if tool_name not in self._computer_names:
            return (
                f"unknown computer tool {tool_name}. "
                "Grep the heading in the computer-use tools.md, then read that section."
            )
        try:
            out = await self._call_computer_once(tool_name, tool_args, session_dir)
        except RuntimeError as exc:
            if not _mcp_died(exc):
                raise
            logging.warning("[plugins] cua-driver MCP died (%s); restarting", exc)
            await self._restart_computer()
            if tool_name not in self._computer_names:
                return f"unknown computer tool {tool_name}"
            out = await self._call_computer_once(tool_name, tool_args, session_dir)
        if _needs_cua_permission(_tool_text(out)):
            await self._prompt_cua_permissions()
            out = await self._call_computer_once(tool_name, tool_args, session_dir)
        return out

    async def _prompt_cua_permissions(self) -> None:
        cmd = computer_mcp_command()
        if not cmd:
            return
        events.emit(
            "notice",
            text=(
                "系统会弹出 **CuaDriver** 的授权。请点允许。"
                "辅助功能和屏幕录制都要点。点完这一步会自己继续。"
            ),
        )
        logging.info("[plugins] requesting CuaDriver permissions")
        proc = await asyncio.create_subprocess_exec(
            cmd[0],
            "permissions",
            "grant",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 180
        try:
            while proc.returncode is None and time.monotonic() < deadline:
                if self._aborted():
                    proc.kill()
                    return
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
            if proc.returncode is None:
                proc.kill()
        except Exception:
            logging.warning("[plugins] cua-driver permissions grant failed", exc_info=True)

    async def _call_computer_once(
        self, tool_name: str, tool_args: Dict[str, Any], session_dir: Optional[str]
    ) -> Any:
        client = self._computer
        if client is None or not client.alive:
            raise RuntimeError("computer-use plugin is not running")
        if self._aborted():
            raise RuntimeError("aborted")
        request = asyncio.create_task(
            client.request(
                "tools/call",
                {"name": tool_name, "arguments": tool_args},
                timeout=_RPC_TIMEOUT,
            )
        )
        abort = self._abort
        if abort is None:
            result = await request
        else:
            waiter = asyncio.create_task(abort.wait())
            _done, _pending = await asyncio.wait(
                {request, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in _pending:
                task.cancel()
            if abort.aborted:
                raise RuntimeError("aborted")
            result = await request
        try:
            out = _mcp_result_to_output(result, session_dir=session_dir, folder="computer")
        except RuntimeError as exc:
            return str(exc)
        if tool_name == "list_windows":
            summary = _format_window_list(result, narrowed=bool((tool_args or {}).get("pid")))
            if summary:
                if isinstance(out, ToolOutput):
                    out.content = summary
                else:
                    out = summary
        text = out.content if isinstance(out, ToolOutput) else str(out)
        clipped = _clip_exec_text(text, session_dir, folder="computer")
        if isinstance(out, ToolOutput):
            out.content = clipped
            return out
        return clipped


_SCREENSHOT_MAX_DIM = 1280


def _shrink_screenshot(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """大图的 base64 会把 stdio MCP 撑死。默认把截图长边压到 1280。"""
    if name != BROWSER_SCREENSHOT or args.get("max_dim"):
        return args
    return {**args, "max_dim": _SCREENSHOT_MAX_DIM}


def _mcp_died(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "not running" in text or "server closed" in text or "mcp server closed" in text


def _needs_inspect_page(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        needle in lower
        for needle in (
            "didn't come up",
            "did not come up",
            "turned off for this browser instance",
            "devtoolsactiveport not found",
            "enable chrome://inspect",
        )
    )


def _needs_cua_permission(text: str) -> bool:
    lower = (text or "").lower()
    return "permissions_pending" in lower or (
        "accessibility" in lower and "screen recording" in lower
    )


_SKIP_WINDOW_APPS = {
    "WindowManager",
    "CursorUIViewService",
    "ThemeWidgetControlViewService",
    "Open and Save Panel Service",
}


def _window_on_screen(item: Dict[str, Any]) -> bool:
    return bool(item.get("is_on_screen") or item.get("on_current_space"))


def _front_window(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    def rank(item: Dict[str, Any]) -> tuple:
        z = item.get("z_index")
        z_rank = z if isinstance(z, int) else -1
        return (z_rank, 1 if _window_on_screen(item) else 0)
    return max(items, key=rank)


def _format_window_list(result: Any, narrowed: bool = False) -> str:
    """没有指定进程时：当前屏幕上、有标题、每个应用只留最前面一个窗口。"""
    if not isinstance(result, dict):
        return ""
    structured = result.get("structuredContent") or result.get("structured_content") or {}
    if not isinstance(structured, dict):
        return ""
    windows = structured.get("windows")
    if not isinstance(windows, list):
        return ""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in windows:
        if not isinstance(item, dict):
            continue
        app = str(item.get("app_name") or "").strip()
        title = str(item.get("title") or "").strip()
        if not app or app in _SKIP_WINDOW_APPS or not title:
            continue
        if not narrowed and not _window_on_screen(item):
            continue
        grouped.setdefault(app, []).append(item)
    chosen = []
    for app, items in grouped.items():
        if narrowed:
            chosen.extend(items)
        else:
            chosen.append(_front_window(items))
    chosen.sort(key=lambda item: str(item.get("app_name") or ""))
    lines = [f"{len(chosen)} window(s)."]
    if not narrowed:
        lines[0] += " One on-screen window per app. Pass pid for the rest."
    for item in chosen:
        app = str(item.get("app_name") or "").strip() or "?"
        title = str(item.get("title") or "").strip() or "-"
        lines.append(f"{app}  pid={item.get('pid')}  window_id={item.get('window_id')}  {title}")
    return "\n".join(lines)


def _tool_text(out: Any) -> str:
    if isinstance(out, ToolOutput):
        return str(out.content or "")
    return str(out or "")


def _needs_remote_debugging_setup(text: str) -> bool:
    lower = (text or "").lower()
    if _needs_inspect_page(text):
        return True
    return any(
        needle in lower
        for needle in (
            "allow remote debugging",
            "permission-blocked",
            "handshake-wait",
            "mac-approve",
        )
    )


# chrome:// 页不允许 AppleScript 执行 JS。复选框在网页里，只能走辅助功能树勾上。
# 不点 Allow 按钮，那一下留给用户。
_TICK_CHECKBOX = r'''
using terms from application "System Events"
    on tickBox(nodeRef)
        try
            if (role of nodeRef as text) is "AXCheckBox" then
                set labelText to ""
                try
                    set labelText to name of nodeRef as text
                end try
                if labelText is "" then
                    try
                        set labelText to description of nodeRef as text
                    end try
                end if
                if labelText contains "Allow remote debugging" then
                    set boxValue to 0
                    try
                        set boxValue to value of nodeRef as integer
                    end try
                    if boxValue is 0 then
                        perform action "AXPress" of nodeRef
                    end if
                    return "ticked"
                end if
            end if
        end try
        try
            repeat with childRef in UI elements of nodeRef
                set childResult to my tickBox(childRef)
                if childResult is "ticked" then return "ticked"
            end repeat
        end try
        return "no"
    end tickBox
end using terms from

tell application "Google Chrome"
    activate
    open location "chrome://inspect/#remote-debugging"
end tell
delay 1.2
set resultText to "not-found"
tell application "System Events"
    if exists process "Google Chrome" then
        tell process "Google Chrome"
            set frontmost to true
            repeat with w in windows
                if my tickBox(w) is "ticked" then
                    set resultText to "ticked"
                    exit repeat
                end if
            end repeat
        end tell
    end if
end tell
return resultText
'''


def enable_remote_debugging_checkbox() -> bool:
    """勾上检查页的远程调试复选框。点不到就返回 False，不假装成功。"""
    if sys.platform != "darwin":
        return False
    for _ in range(3):
        try:
            proc = subprocess.run(
                ["osascript"],
                input=_TICK_CHECKBOX,
                text=True,
                capture_output=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        status = (proc.stdout or "").strip()
        if proc.returncode == 0 and status == "ticked":
            return True
        detail = (proc.stderr or "").strip()
        if "not authorized" in detail.lower() or "assistive" in detail.lower():
            logging.info("[plugins] checkbox needs Accessibility permission: %s", detail)
            return False
        time.sleep(0.6)
    logging.info("[plugins] checkbox not ticked")
    return False


def open_chrome_inspect() -> bool:
    """已有检查页就切过去，不要再 open location 开新标签。"""
    url = "chrome://inspect/#remote-debugging"
    if sys.platform == "darwin":
        script = f'''
tell application "Google Chrome"
  activate
  set targetURL to "{url}"
  repeat with w in windows
    set i to 0
    repeat with t in tabs of w
      set i to i + 1
      try
        if (URL of t as text) contains "chrome://inspect" then
          set active tab index of w to i
          set index of w to 1
          return "reused"
        end if
      end try
    end repeat
  end repeat
  open location targetURL
  return "opened"
end tell
'''
        try:
            proc = subprocess.run(
                ["osascript"],
                input=script,
                text=True,
                capture_output=True,
                timeout=5,
            )
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        import webbrowser

        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def _chrome_remote_debugging_toggled() -> bool:
    path = Path.home() / "Library/Application Support/Google/Chrome/Local State"
    try:
        state = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return False
    enabled = ((state.get("devtools") or {}).get("remote_debugging") or {}).get("user-enabled")
    return enabled is True


def _reopen_log(handle, path: Path):
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass
    return open(path, "ab")


def _plugin_env() -> Dict[str, str]:
    env = os.environ.copy()
    home_bin = str(spark_home() / "bin")
    env["PATH"] = home_bin + os.pathsep + env.get("PATH", "")
    return env


def computer_mcp_command() -> Optional[List[str]]:
    home_bin = spark_home() / "bin" / "cua-driver"
    if home_bin.is_file():
        return [str(home_bin), "mcp"]
    # pytest 把 PERMANENT_HOME 指到空目录；不要去 PATH 上抓真机的 cua-driver。
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    local = Path.home() / ".local" / "bin" / "cua-driver"
    if local.is_file():
        return [str(local), "mcp"]
    found = shutil.which("cua-driver")
    if found:
        return [found, "mcp"]
    return None


def _computer_schema(catalog: Path) -> Dict[str, Any]:
    path = catalog.as_posix()
    return {
        "type": "function",
        "function": {
            "name": COMPUTER,
            "description": (
                "Call one Cua Driver tool on this desktop. "
                f"Names and parameters are in {path}. "
                "Grep a heading such as '# click', then read only that section. "
                "Do not read the whole file. "
                "Web pages use browser_exec, not this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Tool name from tools.md, such as list_windows or click.",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments for that tool. Use {} when it takes none.",
                    },
                },
                "required": ["name"],
            },
        },
    }


def render_computer_tools_md(tools: List[Dict[str, Any]]) -> str:
    lines = [
        "Cua Driver tools. Each section heading is the tool name.",
        "Grep that heading, then read only that section.",
        "",
    ]
    for item in tools:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        desc = str(item.get("description") or "").strip()
        schema = item.get("inputSchema") or item.get("input_schema") or {"type": "object"}
        lines.append(f"# {name}")
        lines.append("")
        if desc:
            lines.append(desc)
            lines.append("")
        lines.append("parameters:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(schema, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_computer_catalog(tools: List[Dict[str, Any]]) -> Path:
    from agent_loop.plugins.pack import ensure_user_plugins

    dest = ensure_user_plugins() / "computer-use"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "tools.md"
    path.write_text(render_computer_tools_md(tools), encoding="utf-8")
    return path


def _computer_call_args(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
    name = str(args.get("name") or "").strip()
    raw = args.get("arguments")
    if raw is None or raw == "":
        raw = {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return "", {}, "computer arguments must be an object"
    if not isinstance(raw, dict):
        return "", {}, "computer arguments must be an object"
    if not name:
        return "", {}, "computer requires name"
    return name, raw, ""


def browser_use_mcp_command() -> Optional[List[str]]:
    home_bin = spark_home() / "bin" / "browser-use"
    if home_bin.is_file():
        return [str(home_bin), "--cli-mcp"]
    # pytest 把 PERMANENT_HOME 指到空目录；不要去 PATH 上抓真机的 browser-use。
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    found = shutil.which("browser-use")
    if found:
        return [found, "--cli-mcp"]
    if shutil.which("uvx"):
        return ["uvx", "--python", "3.12", "browser-use@latest", "--cli-mcp"]
    return None


def _openai_schema(mcp_tool: Dict[str, Any]) -> Dict[str, Any]:
    name = mcp_tool.get("name")
    desc = mcp_tool.get("description") or ""
    params = mcp_tool.get("inputSchema") or mcp_tool.get("input_schema") or {"type": "object"}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": params,
        },
    }


def _clip_exec_text(text: str, session_dir: Optional[str] = None, folder: str = "browser") -> str:
    raw = text or ""
    n_bytes = len(raw.encode("utf-8"))
    if n_bytes <= _EXEC_MAX_BYTES:
        return raw
    saved = ""
    if session_dir:
        dest_dir = Path(session_dir) / "workspace" / "tools_result" / folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"exec-{uuid.uuid4().hex}.txt"
        dest.write_text(raw, encoding="utf-8")
        saved = str(dest)
    preview = raw[:_EXEC_HEAD_CHARS]
    extra = f"\n... truncated ({n_bytes} bytes)"
    if saved:
        extra += f"; full output: {saved}"
    return preview + extra


def _mcp_result_to_output(
    result: Any, session_dir: Optional[str] = None, folder: str = "browser"
) -> Any:
    if not isinstance(result, dict):
        return str(result or "")
    is_error = bool(result.get("isError") or result.get("is_error"))
    chunks: List[str] = []
    image_path = None
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            chunks.append(str(block.get("text") or ""))
        elif kind == "image":
            data = block.get("data") or ""
            mime = str(block.get("mimeType") or block.get("mime_type") or "image/png")
            image_path = _save_image(data, mime, session_dir, folder=folder)
            chunks.append(f"saved: {image_path}")
    text = "\n".join(part for part in chunks if part).strip() or "(no output)"
    if is_error:
        raise RuntimeError(text)
    if image_path:
        return ToolOutput(
            content=text,
            media={"kind": "image", "mime": "image/png", "path": str(image_path)},
        )
    return text


def _save_image(
    data_b64: str, mime: str, session_dir: Optional[str], folder: str = "browser"
) -> Path:
    raw = base64.b64decode(data_b64)
    if session_dir:
        dest_dir = Path(session_dir) / "workspace" / "tools_result" / folder
    else:
        dest_dir = Path("/tmp") / f"permanent-{folder}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}.png"
    dest.write_bytes(raw)
    return dest
