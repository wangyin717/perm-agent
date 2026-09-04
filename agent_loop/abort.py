"""可取消的等待。Ctrl+C 会 abort()，打断 LLM 重试退避。"""

from __future__ import annotations

import asyncio


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
