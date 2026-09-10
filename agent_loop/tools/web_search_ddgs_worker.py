"""DuckDuckGo 搜索子进程入口。stdin 一份 JSON，stdout 一份信封。

ddgs / primp 可能在原生代码里卡住还握着 GIL，必须丢到独立进程里，
父进程才能用墙钟超时把它杀掉。不要在 agent 进程里直接调 DDGS()。
"""

from __future__ import annotations

import json
import sys


def _fail(error: str, code: int) -> int:
    json.dump({"ok": False, "error": error}, sys.stdout)
    sys.stdout.flush()
    return code


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except Exception as exc:
        return _fail(f"invalid request: {exc}", 2)
    query = str(request.get("query") or "")
    max_results = max(1, int(request.get("max_results") or 1))
    if not query.strip():
        return _fail("query 为空", 2)
    try:
        from ddgs import DDGS
    except ImportError:
        return _fail("ddgs package is not installed; pip install ddgs", 1)
    try:
        hits = []
        with DDGS(timeout=10) as client:
            for hit in client.text(query, max_results=max_results):
                hits.append(
                    {
                        "title": str(hit.get("title") or ""),
                        "url": str(hit.get("href") or hit.get("url") or ""),
                        "snippet": str(hit.get("body") or ""),
                    }
                )
                if len(hits) >= max_results:
                    break
        json.dump({"ok": True, "results": hits}, sys.stdout)
        sys.stdout.flush()
        return 0
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}", 1)


if __name__ == "__main__":
    raise SystemExit(main())
