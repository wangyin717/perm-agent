"""本地会话日志：workspace/.agent/sessions/<session_id>.jsonl

一行一次写入。两种 kind：
  record — 执行意图（tool_started），不进模型上下文
  entry  — 对话内容（user / assistant / tool_result）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List


def session_log_path(user_action_data: Dict[str, Any]) -> Path:
    session_id = str(user_action_data.get("sessionId") or "default")
    # 有 workspace 就写到工程里；没有则退回当前目录（本地直接跑 loop）
    root = user_action_data.get("workspace") or os.getcwd()
    return Path(root) / ".agent" / "sessions" / f"{session_id}.jsonl"


class SessionLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_entry(self, entry_type: str, **fields: Any) -> None:
        self._write({"kind": "entry", "type": entry_type, **fields})

    def append_record(self, record_type: str, **fields: Any) -> None:
        self._write({"kind": "record", "type": record_type, **fields})

    def _write(self, row: Dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            f.flush()

    def read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
