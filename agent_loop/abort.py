"""可取消的等待。Ctrl+C 会 abort()，打断 LLM 重试退避和还在等的工具。"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Optional


class RetryCancelledError(Exception):
    def __init__(self, message: str = "Retry cancelled"):
        super().__init__(message)


class Abort:
    def __init__(self):
        self._event = asyncio.Event()

    def abort(self) -> None:
        self._event.set()

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


_current: contextvars.ContextVar[Optional[Abort]] = contextvars.ContextVar(
    "current_abort", default=None
)


def bind_abort(abort: Optional[Abort]):
    return _current.set(abort)


def reset_abort(token) -> None:
    _current.reset(token)


def current_abort() -> Optional[Abort]:
    return _current.get()
