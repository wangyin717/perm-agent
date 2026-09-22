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
        self._schemas: List[Dict[str, Any]] = []
        self._tools: Dict[str, _PluginTool] = {}
        self._started = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._log_file = None
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
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        path = self._session_dir / "browser-use-mcp.log"
        self._log_file = open(path, "ab")
        if self._client is not None:
            self._client.set_stderr_file(self._log_file)

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._started.is_set():
                return
            try:
                await self._start_unlocked()
            finally:
                self._started.set()

    async def stop(self) -> None:
        client = self._client
        self._client = None
        self._schemas = []
        self._tools = {}
        if client is not None:
            await client.close()
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def stop_sync(self) -> None:
        client = self._client
        self._client = None
        if client is not None and client._proc is not None:
            proc = client._proc
            try:
                proc.terminate()
            except Exception:
                pass
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    async def call_tool(self, name: str, args: Dict[str, Any], session_dir: Optional[str] = None) -> Any:
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
            self._started.clear()
            try:
                await self._start_unlocked()
            finally:
                self._started.set()

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
        request = asyncio.create_task(
            self._client.request(
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
        if not plugin_enabled("browser-use", default=True):
            return
        cmd = browser_use_mcp_command()
        if not cmd:
            logging.warning("[plugins] browser-use CLI not found; plugin skipped")
            return
        if self._session_dir is not None:
            self.set_session_dir(self._session_dir)
        env = os.environ.copy()
        home_bin = str(spark_home() / "bin")
        env["PATH"] = home_bin + os.pathsep + env.get("PATH", "")
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
        self._schemas = schemas
        self._tools = adapters
        logging.info("[plugins] browser-use MCP up tools=%s", list(adapters))


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


def _clip_exec_text(text: str, session_dir: Optional[str] = None) -> str:
    raw = text or ""
    n_bytes = len(raw.encode("utf-8"))
    if n_bytes <= _EXEC_MAX_BYTES:
        return raw
    saved = ""
    if session_dir:
        dest_dir = Path(session_dir) / "workspace" / "tools_result" / "browser"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"exec-{uuid.uuid4().hex}.txt"
        dest.write_text(raw, encoding="utf-8")
        saved = str(dest)
    preview = raw[:_EXEC_HEAD_CHARS]
    extra = f"\n... truncated ({n_bytes} bytes)"
    if saved:
        extra += f"; full output: {saved}"
    return preview + extra


def _mcp_result_to_output(result: Any, session_dir: Optional[str] = None) -> Any:
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
            image_path = _save_image(data, mime, session_dir)
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


def _save_image(data_b64: str, mime: str, session_dir: Optional[str]) -> Path:
    raw = base64.b64decode(data_b64)
    if session_dir:
        dest_dir = Path(session_dir) / "workspace" / "tools_result" / "browser"
    else:
        dest_dir = Path("/tmp") / "permanent-browser"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}.png"
    dest.write_bytes(raw)
    return dest
