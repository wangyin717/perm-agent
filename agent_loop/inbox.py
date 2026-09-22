"""用户话两入口：steer 插队（当前 step 完后进下一轮 LLM），follow_up 排队（内层停稳再进）。"""

from __future__ import annotations

from collections import deque
from typing import Any, List, Optional, Tuple


class UserInbox:
    def __init__(self):
        self._steer: deque[Tuple[str, List[Any]]] = deque()
        self._follow_up: deque[Tuple[str, str, List[Any]]] = deque()
        self._seq = 0

    def push_steer(self, text: str, media: Optional[List[Any]] = None) -> None:
        text = (text or "").strip()
        if text:
            self._steer.append((text, list(media or [])))

    def push_follow_up(self, text: str, media: Optional[List[Any]] = None) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        self._seq += 1
        qid = f"q{self._seq}"
        self._follow_up.append((qid, text, list(media or [])))
        return qid

    def cancel_follow_up(self, qid: str) -> bool:
        return self.take_follow_up(qid) is not None

    def take_follow_up(self, qid: str) -> Optional[Tuple[str, List[Any]]]:
        kept: deque[Tuple[str, str, List[Any]]] = deque()
        found: Optional[Tuple[str, List[Any]]] = None
        for item_id, text, media in self._follow_up:
            if item_id == qid and found is None:
                found = (text, media)
                continue
            kept.append((item_id, text, media))
        self._follow_up = kept
        return found

    def has_steer(self) -> bool:
        return bool(self._steer)

    def drain_steer(self) -> List[Tuple[str, List[Any]]]:
        items = list(self._steer)
        self._steer.clear()
        return items

    def drain_follow_up(self) -> List[Tuple[str, List[Any]]]:
        items = [(text, media) for _, text, media in self._follow_up]
        self._follow_up.clear()
        return items
