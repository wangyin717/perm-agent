"""把 Python logging 写到当前会话目录 permanent.log。"""

from __future__ import annotations

import logging
from pathlib import Path

_MARKER = "_permanent_file_log"


def configure_file_logging(session_dir: Path) -> Path:
    log_dir = Path(session_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "permanent.log"
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        if not getattr(handler, _MARKER, False):
            continue
        current = Path(getattr(handler, "baseFilename", "") or "")
        if current.resolve() == path.resolve():
            return path
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    handler = logging.FileHandler(path, encoding="utf-8")
    setattr(handler, _MARKER, True)
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    return path
