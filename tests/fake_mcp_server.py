"""测试用 MCP stdio 服务：一行一条 JSON（与 Python MCP SDK 一致）。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _read_msg():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    decoded = line.decode("utf-8", errors="replace").strip()
    if not decoded:
        return _read_msg()
    return json.loads(decoded)


def _write_msg(obj):
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def main() -> None:
    while True:
        msg = _read_msg()
        if msg is None:
            return
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            _write_msg(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake", "version": "0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if os.environ.get("FAKE_MCP_PROFILE") == "computer":
                _write_msg(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "tools": [
                                {
                                    "name": "list_windows",
                                    "description": (
                                        "x" * 80000
                                        if os.environ.get("MCP_BIG_LINE")
                                        else "List top-level windows."
                                    ),
                                    "inputSchema": {"type": "object", "properties": {}},
                                },
                                {
                                    "name": "click",
                                    "description": "Click one element.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "pid": {"type": "integer"},
                                            "window_id": {"type": "integer"},
                                            "element_index": {"type": "integer"},
                                        },
                                        "required": ["pid", "window_id", "element_index"],
                                    },
                                },
                                {
                                    "name": "get_window_state",
                                    "description": "Read a window.",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "pid": {"type": "integer"},
                                            "window_id": {"type": "integer"},
                                        },
                                        "required": ["pid", "window_id"],
                                    },
                                },
                            ]
                        },
                    }
                )
                continue
            _write_msg(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "tools": [
                            {
                                "name": "browser_exec",
                                "description": "Execute Python in the browser-harness session.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "code": {"type": "string", "description": "Python code"}
                                    },
                                    "required": ["code"],
                                },
                            },
                            {
                                "name": "browser_screenshot",
                                "description": "Capture the current page.",
                                "inputSchema": {"type": "object", "properties": {}},
                            },
                        ]
                    },
                }
            )
        elif method == "tools/call":
            fail_once = os.environ.get("MCP_FAIL_ONCE") or ""
            if fail_once:
                flag = Path(fail_once)
                if not flag.exists():
                    flag.parent.mkdir(parents=True, exist_ok=True)
                    flag.write_text("1", encoding="utf-8")
                    _write_msg(
                        {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "RuntimeError: daemon default didn't come up -- check /tmp/bu.log",
                                    }
                                ],
                                "isError": False,
                            },
                        }
                    )
                    continue
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "browser_exec":
                text = "out:" + str(args.get("code") or "")
                content = [{"type": "text", "text": text}]
            elif name == "list_windows":
                content = [{"type": "text", "text": "window_id=7 pid=4"}]
            elif name == "click":
                content = [
                    {
                        "type": "text",
                        "text": f"clicked {args.get('element_index')}",
                    }
                ]
            elif name == "get_window_state":
                import base64

                png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode("ascii")
                content = [
                    {"type": "text", "text": "elements: 2"},
                    {"type": "image", "data": png, "mimeType": "image/png"},
                ]
            elif name == "browser_screenshot":
                import base64

                png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode("ascii")
                content = [{"type": "image", "data": png, "mimeType": "image/png"}]
            else:
                _write_msg(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": "unknown"}],
                            "isError": True,
                        },
                    }
                )
                continue
            _write_msg(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": content, "isError": False},
                }
            )


if __name__ == "__main__":
    main()
