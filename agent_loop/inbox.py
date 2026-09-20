"""用户话两入口：steer 插队（当前 step 完后进下一轮 LLM），follow_up 排队（内层停稳再进）。"""

from __future__ import annotations

from collections import deque
from typing import Any, List, Optional, Tuple


class UserInbox:
    def __init__(self):
        self._steer: deque[Tuple[str, List[Any]]] = deque()
        self._follow_up: deque[str] = deque()

    def push_steer(self, text: str, media: Optional[List[Any]] = None) -> None:
        text = (text or "").strip()
        if text:
            self._steer.append((text, list(media or [])))

    def push_follow_up(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._follow_up.append(text)

    def has_steer(self) -> bool:
        return bool(self._steer)

    def drain_steer(self) -> List[Tuple[str, List[Any]]]:
        items = list(self._steer)
        self._steer.clear()
        return items

    def drain_follow_up(self) -> List[str]:
        items = list(self._follow_up)
        self._follow_up.clear()
        return items
