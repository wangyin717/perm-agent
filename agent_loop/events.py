"""内核对外的事件口。TUI / CLI 订阅；loop 不 import 任何界面。"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

Listener = Callable[[Dict[str, Any]], None]

_listeners: List[Listener] = []
# CLI 默认仍往 stdout 打；TUI 关掉，只靠订阅者画界面。
print_to_stdout = True


def subscribe(fn: Listener) -> Callable[[], None]:
    _listeners.append(fn)

    def unsubscribe() -> None:
        try:
            _listeners.remove(fn)
        except ValueError:
            pass

    return unsubscribe


def emit(kind: str, **fields: Any) -> None:
    event = {"kind": kind, **fields}
    for fn in list(_listeners):
        fn(event)
