"""子进程里跑一段 Python。只用标准库。

源码从 stdin 读，包成 async def __script()。工具调用走 CODEMODE_FD
上的一行一个 JSON，结果只写 stdout。

挡得住 import、open 和下划线属性，但不是完整的 CPython 沙箱。
"""

from __future__ import annotations

import ast
import asyncio
import json
import math
import os
import re
import socket
import sys
import textwrap
import threading
from types import SimpleNamespace
from typing import Any, Optional

_BANNED_CALLS = frozenset(
    {
        "__import__",
        "open",
        "exec",
        "eval",
        "compile",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "input",
        "help",
        "dir",
        "memoryview",
    }
)

_KEEP_BUILTINS = (
    "len",
    "range",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "enumerate",
    "zip",
    "map",
    "filter",
    "sorted",
    "min",
    "max",
    "sum",
    "any",
    "all",
    "abs",
    "round",
    "repr",
    "isinstance",
    "chr",
    "ord",
    "type",
    "reversed",
    "iter",
    "next",
    "divmod",
    "pow",
    "hex",
    "bin",
    "oct",
    "ascii",
    "format",
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
)


class _ScriptDone(BaseException):
    def __init__(self, value: Any = None) -> None:
        self.value = value


class _Tools:
    def __init__(self, rpc: "_Rpc") -> None:
        self._rpc = rpc

    def __getattr__(self, name: str):
        if not name.isidentifier() or name.startswith("_"):
            raise AttributeError(name)
        rpc = self._rpc

        async def call(**kwargs: Any) -> str:
            return await rpc.call(name, kwargs)

        return call


class _Rpc:
    def __init__(self) -> None:
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: dict = {}
        self._write_lock = threading.Lock()
        self._ids = 0

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        raw = os.environ.get("CODEMODE_FD")
        if not raw:
            return
        self._sock = socket.socket(fileno=int(raw))
        self._sock.setblocking(True)
        self._thread = threading.Thread(
            target=self._read_loop, name="codemode-rpc", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._fail_pending()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    async def call(self, name: str, args: dict) -> str:
        if self._loop is None or self._sock is None:
            raise RuntimeError("tool bridge is unavailable")
        self._ids += 1
        req_id = self._ids
        fut = self._loop.create_future()
        self._pending[req_id] = fut
        payload = (
            json.dumps(
                {"id": req_id, "op": "call", "name": name, "args": args},
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
            + b"\n"
        )
        try:
            await asyncio.to_thread(self._send, payload)
            msg = await fut
        finally:
            self._pending.pop(req_id, None)
        if not isinstance(msg, dict) or not msg.get("ok"):
            error = ""
            if isinstance(msg, dict) and msg.get("error"):
                error = str(msg.get("error"))
            raise RuntimeError(error or "tool failed")
        content = msg.get("content")
        if content is None:
            return ""
        return content if isinstance(content, str) else str(content)

    def _send(self, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise RuntimeError("tool bridge is closed")
        with self._write_lock:
            sock.sendall(payload)

    def _read_loop(self) -> None:
        sock = self._sock
        buf = bytearray()
        try:
            while sock is not None:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    self._schedule(self._deliver, line)
        finally:
            self._schedule(self._fail_pending)

    def _schedule(self, fn, *args) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass

    def _deliver(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(msg, dict):
            return
        fut = self._pending.get(msg.get("id"))
        if fut is None or fut.done():
            return
        fut.set_result(msg)

    def _fail_pending(self) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("tool connection closed"))


def _make_print(console: list):
    def print(*args, sep=" ", end="\n", file=None, flush=False):
        if file is not None:
            raise TypeError("print() file argument is disabled")
        console.append(sep.join(str(arg) for arg in args) + end)

    return print


def _exit(value: Any = None) -> None:
    raise _ScriptDone(value)


def _safe_builtins(console: list) -> dict:
    import builtins

    safe = {name: getattr(builtins, name) for name in _KEEP_BUILTINS}
    safe["print"] = _make_print(console)
    safe["exit"] = _exit
    return safe


def _asyncio_facade() -> SimpleNamespace:
    return SimpleNamespace(
        gather=asyncio.gather,
        sleep=asyncio.sleep,
        wait=asyncio.wait,
        TimeoutError=asyncio.TimeoutError,
    )


def _namespace(console: list, rpc: _Rpc) -> dict:
    safe = _safe_builtins(console)
    return {
        "__builtins__": safe,
        "json": json,
        "re": re,
        "math": math,
        "asyncio": _asyncio_facade(),
        "tools": _Tools(rpc),
        "print": safe["print"],
        "exit": safe["exit"],
    }


def _wrap(source: str) -> str:
    body = textwrap.indent(source, "    ")
    if not body.endswith("\n"):
        body += "\n"
    return "async def __script():\n" + body


def _called_name(node: ast.AST) -> str:
    if not isinstance(node, ast.Call):
        return ""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _reject(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("import is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"attribute {node.attr} is not allowed")
        called = _called_name(node)
        if called in _BANNED_CALLS:
            raise ValueError(f"call to {called} is not allowed")


def _ok(console: list, value: Any) -> dict:
    return {"ok": True, "output": value, "console": "".join(console), "error": None}


def _fail(console: list, error: str) -> dict:
    return {"ok": False, "output": None, "console": "".join(console), "error": error}


async def _eval(source: str, rpc: _Rpc, console: list) -> dict:
    try:
        tree = ast.parse(_wrap(source), filename="<codemode>")
    except SyntaxError as exc:
        return _fail(console, f"SyntaxError: {exc}")
    try:
        _reject(tree)
    except ValueError as exc:
        return _fail(console, str(exc))
    ns = _namespace(console, rpc)
    try:
        exec(compile(tree, "<codemode>", "exec"), ns)
        value = await ns["__script"]()
    except _ScriptDone as done:
        return _ok(console, done.value)
    except Exception as exc:
        return _fail(console, f"{type(exc).__name__}: {exc}")
    return _ok(console, value)


async def _run(source: str) -> dict:
    rpc = _Rpc()
    console: list[str] = []
    try:
        rpc.start()
        return await _eval(source, rpc, console)
    finally:
        rpc.close()


def _emit(ok: bool, output: Any, console: str, error: Optional[str]) -> None:
    payload = {"ok": bool(ok), "output": output, "console": console or "", "error": error}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> int:
    try:
        source = sys.stdin.buffer.read().decode("utf-8")
    except Exception as exc:
        _emit(False, None, "", f"{type(exc).__name__}: {exc}")
        return 0
    try:
        result = asyncio.run(_run(source))
    except Exception as exc:
        _emit(False, None, "", f"{type(exc).__name__}: {exc}")
        return 0
    _emit(
        bool(result.get("ok")),
        result.get("output"),
        str(result.get("console") or ""),
        result.get("error"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
