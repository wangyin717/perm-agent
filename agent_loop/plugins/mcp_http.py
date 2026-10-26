"""网页容器里的 MCP 客户端。请求头带短时令牌，不把密钥放进子进程。"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import aiohttp


class McpHttpClient:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self._next_id = 1

    @property
    def alive(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None, *, timeout: float = 30) -> Any:
        msg_id = self._next_id
        self._next_id += 1
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.post(self.url, headers=headers, data=json.dumps(payload).encode()) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"MCP HTTP {response.status} {text[:200]}")
                message = json.loads(text) if text else {}
        if not isinstance(message, dict):
            raise RuntimeError("MCP HTTP 没有返回 JSON")
        if "error" in message:
            err = message["error"]
            detail = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(detail or "MCP error")
        return message.get("result")
