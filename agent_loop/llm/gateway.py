"""本机 TUI 发现 Magpie 在听，就把模型请求交给它。网页容器走自己的中转，不经过这里。"""

from __future__ import annotations

import socket

MAGPIE_BASE = "http://127.0.0.1:3425/v1"
MAGPIE_KEY = "magpie"


def magpie_listening(timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 3425), timeout):
            return True
    except OSError:
        return False


def apply_magpie(client) -> None:
    if not magpie_listening():
        return
    client.api_base = MAGPIE_BASE
    client.api_key = MAGPIE_KEY
    client.upstream_model = f"{client.provider_id}/{client.model}"
