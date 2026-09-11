"""Memory 文件枚举、短名、按 ## 切块、关键词搜。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from agent_loop.paths import global_memory_path, project_memory_dir

_MEDIUM_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-.+\.md$")
_HEADING = re.compile(r"(?m)^##\s+")


@dataclass
class MemoryFile:
    short: str
    path: Path
    source: str  # project | session | global


def list_memory_files(workspace: str, home: Optional[str] = None) -> List[MemoryFile]:
    """项目长期 → 中期（新的在前）→ 全局。"""
    out: List[MemoryFile] = []
    mem = project_memory_dir(workspace, home)
    project_long = mem / "MEMORY.md"
    if project_long.is_file():
        out.append(MemoryFile("MEMORY.md", project_long, "project"))
    medium = []
    if mem.is_dir():
        for p in mem.iterdir():
            if p.is_file() and _MEDIUM_NAME.match(p.name):
                medium.append(p)
        medium.sort(key=lambda p: p.name, reverse=True)
        for p in medium:
            out.append(MemoryFile(p.name, p, "session"))
    g = global_memory_path(home)
    if g.is_file():
        out.append(MemoryFile("global/MEMORY.md", g, "global"))
    return out


def resolve_short_path(workspace: str, short: str, home: Optional[str] = None) -> Path:
    name = (short or "").strip()
    if not name:
        raise ValueError("path 为空")
    if name in {"global/MEMORY.md", "global"}:
        path = global_memory_path(home)
        if not path.is_file():
            raise FileNotFoundError(name)
        return path.resolve()
    mem = project_memory_dir(workspace, home).resolve()
    if name == "MEMORY.md":
        path = (mem / "MEMORY.md").resolve()
    else:
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("invalid memory path")
        if not _MEDIUM_NAME.match(name) and name != "MEMORY.md":
            raise ValueError("invalid memory path")
        path = (mem / name).resolve()
    try:
        path.relative_to(mem)
    except ValueError as exc:
        raise ValueError("memory path escapes project memory dir") from exc
    if path == mem:
        raise ValueError("invalid memory path")
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def split_chunks(text: str) -> List[Tuple[str, str, int]]:
    """返回 (heading, body, start_line 1-based)。"""
    if not text:
        return []
    lines = text.splitlines()
    if not _HEADING.search(text):
        return [("", text.strip(), 1)]
    chunks: List[Tuple[str, str, int]] = []
    start = 0
    heading = ""
    start_line = 1
    for i, line in enumerate(lines):
        if line.startswith("## "):
            if i > start or heading:
                body = "\n".join(lines[start:i]).strip()
                if body or heading:
                    chunks.append((heading, body, start_line))
            heading = line[3:].strip()
            start = i
            start_line = i + 1
    body = "\n".join(lines[start:]).strip()
    if body or heading:
        chunks.append((heading, body, start_line))
    return chunks or [("", text.strip(), 1)]


def _tokens(query: str) -> List[str]:
    return [t.lower() for t in re.split(r"\s+", query.strip()) if t]


def chunk_matches(body: str, heading: str, tokens: List[str]) -> bool:
    hay = f"{heading}\n{body}".lower()
    return all(tok in hay for tok in tokens)


def snippet(body: str, tokens: List[str], max_lines: int = 4) -> str:
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return ""
    if not tokens:
        return "\n".join(lines[:max_lines])
    idx = 0
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(tok in low for tok in tokens):
            idx = i
            break
    chunk = lines[idx : idx + max_lines]
    return "\n".join(chunk)
