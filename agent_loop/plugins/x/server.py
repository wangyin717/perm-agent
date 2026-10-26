"""本机 X 插件的 MCP 进程。只读 ~/.permanent/.env，网页容器不启动它。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict

from agent_loop.envfile import load_dotenv
from agent_loop.plugins.x.api import TOOLS, call_tool


def _bearer() -> str:
    load_dotenv()
    return (os.environ.get("X_BEARER_TOKEN") or "").strip()


def _ok(msg_id: Any, result: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def _handle(message: Dict[str, Any]) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return
    if method == "initialize":
        _ok(msg_id, {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "perm-x", "version": "0.1"}})
        return
    if method == "tools/list":
        _ok(msg_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = message.get("params") or {}
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        try:
            text = await call_tool(name, args, _bearer())
            _ok(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            _ok(msg_id, {"content": [{"type": "text", "text": str(exc) or "X 请求失败"}], "isError": True})
        return
    _ok(msg_id, {})


async def _main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while True:
        line = await reader.readline()
        if not line:
            return
        decoded = line.decode("utf-8", errors="replace").strip()
        if not decoded:
            continue
        try:
            message = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            await _handle(message)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
