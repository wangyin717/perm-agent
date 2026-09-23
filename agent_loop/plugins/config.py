"""~/.permanent/config.json：插件开关。browser-use 和 computer-use 缺省开启。"""

from __future__ import annotations

import json
from typing import Any, Dict

from agent_loop.paths import spark_home


def load_config() -> Dict[str, Any]:
    path = spark_home() / "config.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def plugin_enabled(name: str, default: bool = True) -> bool:
    plugins = load_config().get("plugins")
    if not isinstance(plugins, dict):
        return default
    if name not in plugins:
        return default
    return bool(plugins[name])
