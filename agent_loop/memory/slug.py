"""中期文件名里的 slug。"""

from __future__ import annotations

import re


def slugify(text: str, max_len: int = 32) -> str:
    raw = (text or "").strip().lower()
    raw = re.sub(r"\s+", "-", raw)
    raw = re.sub(r"[^a-z0-9._-]+", "", raw)
    raw = raw.strip("-._")
    return (raw or "session")[:max_len]
