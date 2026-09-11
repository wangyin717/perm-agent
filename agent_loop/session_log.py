"""会话日志：~/.spark-agent/projects/{slug}-{hash}/chats/<id>/session.jsonl

一行一次写入。两种 kind：
  record — 执行意图（tool_started），不进模型上下文
  entry  — 对话内容（user / assistant / tool_result）
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

from agent_loop import paths as _paths


def session_log_path(user_action_data: Dict[str, Any]) -> Path:
    workspace = user_action_data.get("workspace") or os.getcwd()
    _paths.ensure_project_layout(workspace, user_action_data.get("sparkHome"))
    return _paths.session_log_path(user_action_data)


class SessionLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 并行工具可能同时关单；整行 append 必须互斥，避免 jsonl 行被撕开。
        self._io_lock = threading.Lock()

    def append_entry(self, entry_type: str, **fields: Any) -> None:
        self._write({"kind": "entry", "type": entry_type, **fields})

    def append_record(self, record_type: str, **fields: Any) -> None:
        self._write({"kind": "record", "type": record_type, **fields})

    def _write(self, row: Dict[str, Any]) -> None:
        payload = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with self._io_lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(payload)
                f.flush()

    def read_all(self) -> List[Dict[str, Any]]:
        """seq 按文件中的行号（从 1 开始）现算，不落盘。append-only 保证行号即写入顺序，
        对老日志（没有过 seq 字段的时代写的行）天然兼容，不会因为缺字段被当成 0 而漏读。
        """
        if not self.path.exists():
            return []
        rows = []
        with open(self.path, encoding="utf-8") as f:
            for i, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                row["seq"] = i
                rows.append(row)
        return rows
