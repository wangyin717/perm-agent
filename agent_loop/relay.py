"""网页版这一轮的模型代理。只放在内存里，不进环境变量，也不进会话文件。"""

from __future__ import annotations

from typing import Optional, Tuple

_relay: Optional[Tuple[str, str]] = None


def set_relay(token: str, api_base: str) -> None:
    global _relay
    token = token.strip()
    api_base = api_base.strip().rstrip("/")
    _relay = (token, api_base) if token and api_base else None


def clear_relay() -> None:
    global _relay
    _relay = None


def current_relay() -> Optional[Tuple[str, str]]:
    return _relay
