"""最小 MCP stdio 客户端（Content-Length JSON-RPC）。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

_PROTOCOL = "2024-11-05"
# cua-driver 的 tools/list 是一行 JSON，56 个工具大约 150KB。
# asyncio 默认 readline 上限是 64KB，超了会把连接当成已断开。
_STREAM_LIMIT = 32 * 1024 * 1024


class McpStdioClient:
    def __init__(self, command: list[str], *, env: Optional[Dict[str, str]] = None):
        self.command = list(command)
        self.env = env
        self._stderr_file = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()

    def set_stderr_file(self, handle) -> None:
        self._stderr_file = handle

    @property
    def alive(self) -> bool:
        if self._proc is None or self._proc.returncode is not None:
            return False
        if self._reader_task is not None and self._reader_task.done():
            return False
        return True

    async def start(self) -> None:
        if self.alive:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            start_new_session=True,
            limit=_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name="mcp-stdio-reader")
        self._stderr_task = asyncio.create_task(self._pump_stderr(), name="mcp-stderr")
        await self.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "permanent", "version": "0.1"},
            },
        )
        await self.notify("notifications/initialized", {})

    async def close(self) -> None:
        proc = self._proc
        self._proc = None
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._reader_task = None
        self._stderr_task = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None, *, timeout: float = 30) -> Any:
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise RuntimeError("MCP server is not running")
        async with self._lock:
            msg_id = self._next_id
            self._next_id += 1
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending[msg_id] = fut
            payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
            if params is not None:
                payload["params"] = params
            await self._write(payload)
        return await asyncio.wait_for(fut, timeout=timeout)

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        if not self.alive or self._proc is None:
            raise RuntimeError("MCP server is not running")
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        async with self._lock:
            await self._write(payload)

    async def _write(self, payload: Dict[str, Any]) -> None:
        # Python MCP SDK（browser-use --cli-mcp）走 NDJSON：一行一条 JSON，不是 LSP Content-Length。
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        stdin = self._proc.stdin if self._proc else None
        if stdin is None:
            raise RuntimeError("MCP stdin closed")
        stdin.write(raw)
        await stdin.drain()

    async def _pump_stderr(self) -> None:
        stream = self._proc.stderr if self._proc else None
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                handle = self._stderr_file
                if handle is not None:
                    try:
                        handle.write(chunk)
                        handle.flush()
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.debug("[mcp] stderr pump stopped", exc_info=True)

    async def _read_loop(self) -> None:
        stdout = self._proc.stdout if self._proc else None
        if stdout is None:
            return
        try:
            while True:
                msg = await _read_message(stdout)
                if msg is None:
                    break
                msg_id = msg.get("id")
                if msg_id is None:
                    continue
                fut = self._pending.pop(msg_id, None)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    text = err.get("message") if isinstance(err, dict) else str(err)
                    fut.set_exception(RuntimeError(text or "MCP error"))
                else:
                    fut.set_result(msg.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.debug("[mcp] reader stopped", exc_info=True)
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("MCP server closed"))
            self._pending.clear()


async def _read_message(stream: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    while True:
        line = await stream.readline()
        if not line:
            return None
        decoded = line.decode("utf-8", errors="replace").strip()
        if not decoded:
            continue
        if decoded.lower().startswith("content-length:"):
            # 兼容 LSP 帧：读完头再读 body。
            headers = {"content-length": decoded.split(":", 1)[1].strip()}
            while True:
                next_line = await stream.readline()
                if not next_line or next_line in (b"\r\n", b"\n"):
                    break
                extra = next_line.decode("utf-8", errors="replace").strip()
                if ":" in extra:
                    key, value = extra.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or "0")
            if length <= 0:
                return None
            body = await stream.readexactly(length)
            return json.loads(body.decode("utf-8"))
        return json.loads(decoded)
