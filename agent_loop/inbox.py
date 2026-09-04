"""用户话两入口：steer 插队（当前 step 完后进下一轮 LLM），follow_up 排队（内层停稳再进）。"""

from __future__ import annotations

from collections import deque
from typing import List


class UserInbox:
    def __init__(self):
        self._steer: deque[str] = deque()
        self._follow_up: deque[str] = deque()

    def push_steer(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._steer.append(text)

    def push_follow_up(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._follow_up.append(text)

    def has_steer(self) -> bool:
        return bool(self._steer)

    def drain_steer(self) -> List[str]:
        items = list(self._steer)
        self._steer.clear()
        return items

    def drain_follow_up(self) -> List[str]:
        items = list(self._follow_up)
        self._follow_up.clear()
        return items
