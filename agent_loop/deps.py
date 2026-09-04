"""宿主需要提供的最小能力。CLI 用本地 subprocess 沙箱实现。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExecutionDependencies(Protocol):
    async def ensure_sandbox_async(self) -> Any:
        """返回可用沙箱（要有 commands.run）。"""
        ...
