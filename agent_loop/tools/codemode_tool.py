"""codemode：模型写一段 Python，在子进程里跑，用现有工具。

子进程没有 agent_loop，也不能 import / open / 联网。工具调用回到父进程，
走 ToolRuntime 的准备和执行。内层结果不进会话日志。回到模型的只有脚本的
print 和 return。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

NAME = "codemode"
REPLAY = "never"
READ_ONLY = False
BEFORE_HOOKS: list = []
AFTER_HOOKS: list = []

MAX_CODE_CHARS = 100_000
MAX_RESULT_CHARS = 10_000
DEFAULT_TIMEOUT_MS = 60_000
MIN_TIMEOUT_MS = 1_000
MAX_TIMEOUT_MS = 300_000
_RUNNER = Path(__file__).resolve().with_name("codemode_runner.py")
_LINE_LIMIT = 8 * 1024 * 1024


class _TimedOut(Exception):
    pass


@dataclass
class _Outcome:
    ok: bool
    output: Any
    console: str
    error: Optional[str]


def _bound_runtime():
    # 顶层不能 import tool_runtime：registry 加载本模块时，runtime 还在加载 registry。
    from agent_loop.runtime.tool_runtime import current_runtime

    runtime = current_runtime.get()
    if runtime is None:
        raise RuntimeError("codemode has no ToolRuntime")
    return runtime


def _timeout_ms(raw: Any) -> int:
    if raw is None or raw == "":
        return DEFAULT_TIMEOUT_MS
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("timeout_ms must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("timeout_ms must be an integer") from None
    if value < MIN_TIMEOUT_MS:
        return MIN_TIMEOUT_MS
    if value > MAX_TIMEOUT_MS:
        return MAX_TIMEOUT_MS
    return value


def _require_code(args: dict) -> str:
    code = args.get("code")
    if not isinstance(code, str):
        raise ValueError("code must be a string")
    if len(code) > MAX_CODE_CHARS:
        raise ValueError(f"code is {len(code)} characters; limit is {MAX_CODE_CHARS}")
    return code


def _render_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def format_script_result(
    *,
    ok: bool,
    elapsed: float,
    output: Any,
    console: str,
    error: Optional[str],
) -> str:
    clock = f"{max(elapsed, 0.0):.3f}"
    shown = (console or "").rstrip("\n")
    rendered = _render_output(output)
    if ok:
        parts = [f"Script completed in {clock}s"]
        if rendered:
            parts.append(rendered)
        if shown:
            parts.append(shown)
        return "\n".join(parts)
    parts = [f"Script failed in {clock}s"]
    if shown:
        parts.append(shown)
    if rendered:
        parts.append(rendered)
    parts.append(f"Script error: {error or 'unknown error'}")
    return "\n".join(parts)


def _spill(session_dir: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    dest = Path(session_dir) / "tools_result" / "codemode" / f"{digest}.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return str(dest)


def _pack(text: str, keep: int) -> str:
    if keep <= 0:
        omitted = len(text)
        return f"\n... {omitted} characters omitted ...\n"
    head_n = keep // 2
    tail_n = keep - head_n
    omitted = len(text) - head_n - tail_n
    marker = f"\n... {omitted} characters omitted ...\n"
    return text[:head_n] + marker + text[-tail_n:]


def _head_tail(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    lo = 0
    hi = min(len(text), limit)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        packed = _pack(text, mid)
        if len(packed) <= limit:
            best = packed
            lo = mid + 1
        else:
            hi = mid - 1
    return best or text[:limit]


def cap_text(text: str, session_dir: Optional[str]) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    saved = ""
    if session_dir:
        try:
            saved = _spill(session_dir, text)
        except OSError:
            saved = ""
    footer = f"\nfull output: {saved}" if saved else ""
    if len(footer) >= MAX_RESULT_CHARS:
        footer = ""
    clipped = _head_tail(text, MAX_RESULT_CHARS - len(footer)) + footer
    if len(clipped) > MAX_RESULT_CHARS:
        return clipped[:MAX_RESULT_CHARS]
    return clipped


def _content_or_raise(result) -> str:
    if result.is_error:
        raise RuntimeError(result.content)
    if not isinstance(result.content, str):
        return "" if result.content is None else str(result.content)
    return result.content


async def _call_inner(runtime, name: str, args: dict, sandbox) -> str:
    from agent_loop.runtime.tool_runtime import quiet_records

    if name == NAME:
        raise RuntimeError("do not call codemode from a codemode script")
    tool_call = {
        "id": f"cm-{uuid4().hex}",
        "function": {"name": name, "arguments": args},
    }
    token = quiet_records.set(True)
    try:
        prepared = await runtime._prepare_call(tool_call)
        if prepared.result is not None:
            return _content_or_raise(prepared.result)
        lock = runtime.writer_lock(prepared)
        if lock is None:
            result = await runtime._execute_prepared(prepared, sandbox)
        else:
            async with lock:
                result = await runtime._execute_prepared(prepared, sandbox)
        return _content_or_raise(result)
    finally:
        quiet_records.reset(token)


def _close_sock(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _submit(loop, queue: asyncio.Queue, item) -> None:
    if loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(queue.put_nowait, item)
    except RuntimeError:
        pass


def _reader(sock: socket.socket, loop, queue: asyncio.Queue) -> None:
    buf = bytearray()
    try:
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > _LINE_LIMIT and b"\n" not in buf:
                break
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                _submit(loop, queue, line)
    finally:
        _submit(loop, queue, None)


def _send_all(sock: socket.socket, lock: threading.Lock, data: bytes) -> None:
    with lock:
        sock.sendall(data)


async def _handle_line(line: bytes, sock, send_lock, runtime, sandbox) -> None:
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(msg, dict):
        return
    req_id = msg.get("id")
    try:
        if msg.get("op") != "call":
            raise RuntimeError("bad tool request")
        name = msg.get("name")
        args = msg.get("args")
        if not isinstance(name, str) or not name:
            raise RuntimeError("bad tool request")
        if not isinstance(args, dict):
            raise RuntimeError("args must be an object")
        content = await _call_inner(runtime, name, args, sandbox)
        body = {"id": req_id, "ok": True, "content": content}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        body = {"id": req_id, "ok": False, "error": str(exc) or type(exc).__name__}
    data = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        await asyncio.to_thread(_send_all, sock, send_lock, data)
    except asyncio.CancelledError:
        raise
    except OSError:
        return


async def _pump(queue, sock, send_lock, runtime, sandbox) -> None:
    tasks: set = set()
    try:
        while True:
            line = await queue.get()
            if line is None:
                break
            if not line.strip():
                continue
            task = asyncio.create_task(_handle_line(line, sock, send_lock, runtime, sandbox))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _start_bridge(parent, loop, runtime, sandbox) -> asyncio.Task:
    queue: asyncio.Queue = asyncio.Queue()
    send_lock = threading.Lock()
    thread = threading.Thread(
        target=_reader,
        args=(parent, loop, queue),
        name="codemode-bridge",
        daemon=True,
    )
    thread.start()
    return asyncio.create_task(_pump(queue, parent, send_lock, runtime, sandbox))


async def _spawn(child: socket.socket, workspace: Optional[str]):
    if not _RUNNER.is_file():
        raise RuntimeError(f"codemode runner is missing: {_RUNNER}")
    env = os.environ.copy()
    env["CODEMODE_FD"] = str(child.fileno())
    env["PYTHONUNBUFFERED"] = "1"
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-u",
        str(_RUNNER),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=workspace or None,
        pass_fds=(child.fileno(),),
    )


async def _write_stdin(proc, code: str) -> None:
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(code.encode("utf-8"))
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        proc.stdin.close()
        try:
            await proc.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


async def _read_stream(stream) -> bytes:
    if stream is None:
        return b""
    return await stream.read()


async def _wait_until(awaitable, deadline: float):
    left = deadline - asyncio.get_running_loop().time()
    if left <= 0:
        raise _TimedOut()
    try:
        return await asyncio.wait_for(awaitable, left)
    except asyncio.TimeoutError:
        raise _TimedOut() from None


async def _kill(proc) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await proc.wait()
    except ProcessLookupError:
        pass


def _parse_stdout(raw: bytes) -> Optional[_Outcome]:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "ok" not in data:
        return None
    console = data.get("console") or ""
    if not isinstance(console, str):
        console = str(console)
    error = data.get("error")
    if error is not None and not isinstance(error, str):
        error = str(error)
    return _Outcome(bool(data.get("ok")), data.get("output"), console, error)


def _interpret(code: int, stdout: bytes, stderr: bytes) -> _Outcome:
    parsed = _parse_stdout(stdout)
    if parsed is not None:
        return parsed
    err = stderr.decode("utf-8", errors="replace").strip()
    if code != 0:
        message = err or f"runner exited with code {code}"
    else:
        message = err or "script produced no result"
    return _Outcome(False, None, "", message)


async def _stop_pump(pump) -> None:
    if pump is None or pump.done():
        return
    pump.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass


async def _run_child(code, timeout_ms, runtime, sandbox, workspace) -> _Outcome:
    parent, child = socket.socketpair()
    child.set_inheritable(True)
    proc = None
    pump = None
    try:
        proc = await _spawn(child, workspace)
        child.close()
        child = None
        loop = asyncio.get_running_loop()
        pump = _start_bridge(parent, loop, runtime, sandbox)
        out_task = asyncio.create_task(_read_stream(proc.stdout))
        err_task = asyncio.create_task(_read_stream(proc.stderr))
        await _write_stdin(proc, code)
        deadline = loop.time() + (timeout_ms / 1000)
        await _wait_until(proc.wait(), deadline)
        await _wait_until(pump, deadline)
        pump = None
        stdout = await _wait_until(out_task, deadline)
        stderr = await _wait_until(err_task, deadline)
        code_rc = proc.returncode if proc.returncode is not None else 1
        return _interpret(code_rc, stdout, stderr)
    finally:
        if child is not None:
            child.close()
        _close_sock(parent)
        await _kill(proc)
        await _stop_pump(pump)


async def execute(args, sandbox=None, workspace=None, session_dir=None) -> str:
    code = _require_code(args or {})
    timeout_ms = _timeout_ms((args or {}).get("timeout_ms"))
    runtime = _bound_runtime()
    started = time.monotonic()
    try:
        outcome = await _run_child(code, timeout_ms, runtime, sandbox, workspace)
    except _TimedOut:
        outcome = _Outcome(False, None, "", f"timed out after {timeout_ms}ms")
    except Exception as exc:
        outcome = _Outcome(False, None, "", str(exc) or type(exc).__name__)
    text = format_script_result(
        ok=outcome.ok,
        elapsed=time.monotonic() - started,
        output=outcome.output,
        console=outcome.console,
        error=outcome.error,
    )
    text = cap_text(text, session_dir)
    if not outcome.ok:
        raise RuntimeError(text)
    return text
