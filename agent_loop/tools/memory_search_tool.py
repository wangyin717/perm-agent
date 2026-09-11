"""跨会话 memory 召回。只搜家目录里的 memory 文件，不是仓库 grep。"""

from __future__ import annotations

from typing import Any, Dict, List

from agent_loop.memory.files import (
    chunk_matches,
    list_memory_files,
    resolve_short_path,
    snippet,
    split_chunks,
    _tokens,
)

NAME = "memory_search"
REPLAY = "safe"
READ_ONLY = True

DEFAULT_MAX_RESULTS = 8
MAX_RESULTS_CAP = 20


def before_tool_deny_empty(event: Dict[str, Any]):
    args = event.get("args") or {}
    query = str(args.get("query") or "").strip()
    path = str(args.get("path") or "").strip()
    if not query and not path:
        return {"block": {"reason": "query 或 path 为空"}}
    return None


def after_tool_truncate_output(event: Dict[str, Any]):
    content = event.get("content") or ""
    if len(content) <= 8000:
        return None
    return {"content": content[:8000] + "\n...[output truncated]"}


BEFORE_HOOKS = [before_tool_deny_empty]
AFTER_HOOKS = [after_tool_truncate_output]


def _clamp_max_results(raw: Any) -> int:
    if raw is None or raw == "":
        return DEFAULT_MAX_RESULTS
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESULTS
    return max(1, min(n, MAX_RESULTS_CAP))


async def execute(
    args: Dict[str, Any], sandbox=None, workspace=None, session_dir=None
) -> str:
    query = str(args.get("query") or "").strip()
    path = str(args.get("path") or "").strip()
    max_results = _clamp_max_results(args.get("max_results"))
    root = workspace or "."
    if path and not query:
        return _read_file(root, path, args)
    files = list_memory_files(root)
    if path:
        target = resolve_short_path(root, path)
        files = [f for f in files if f.path.resolve() == target]
        if not files:
            raise FileNotFoundError(path)
    if not query:
        raise ValueError("query 为空")
    tokens = _tokens(query)
    hits: List[str] = []
    scored: List[tuple] = []
    for mf in files:
        try:
            text = mf.path.read_text(encoding="utf-8")
        except OSError:
            continue
        for heading, body, _line in split_chunks(text):
            if not chunk_matches(body, heading, tokens):
                continue
            n = sum(1 for t in tokens if t in f"{heading}\n{body}".lower())
            scored.append((n, mf, heading, body))
    scored.sort(key=lambda row: -row[0])
    for i, (_n, mf, heading, body) in enumerate(scored[:max_results], start=1):
        title = f"## {heading}" if heading else "(no heading)"
        snip = snippet(body, tokens)
        hits.append(f"{i}. [{mf.source}] {mf.short}  {title}\n   {snip.replace(chr(10), chr(10) + '   ')}")
    if not hits:
        return f'No memory matches for "{query}"'
    return "\n\n".join(hits)


def _read_file(workspace: str, short: str, args: Dict[str, Any]) -> str:
    path = resolve_short_path(workspace, short)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    offset = args.get("offset")
    limit = args.get("limit")
    start = 1
    if offset is not None and str(offset).strip() != "":
        try:
            start = max(1, int(offset))
        except (TypeError, ValueError):
            start = 1
    chunk = lines[start - 1 :]
    if limit is not None and str(limit).strip() != "":
        try:
            chunk = chunk[: max(0, int(limit))]
        except (TypeError, ValueError):
            pass
    numbered = [f"{start + i}|{line}" for i, line in enumerate(chunk)]
    header = f"[memory] {short}  ({len(lines)} lines)"
    if not numbered:
        return header + "\n"
    return header + "\n" + "\n".join(numbered)
