"""家目录项目路径。"""
from __future__ import annotations

from pathlib import Path

from agent_loop.paths import project_dir, project_key, session_log_path_for, spark_home
from agent_loop.session_log import session_log_path


def test_project_key_stable_and_safe(tmp_path):
    key = project_key(tmp_path)
    assert key == project_key(tmp_path / ".")
    assert "-" in key
    assert project_key(tmp_path) == project_key(str(tmp_path))


def test_session_log_path_layout(tmp_path):
    home = Path(spark_home())
    path = session_log_path({"sessionId": "wy1", "workspace": str(tmp_path)})
    proj = project_dir(tmp_path)
    assert path == proj / "chats" / "wy1" / "session.jsonl"
    assert (proj / "memory").is_dir()
    assert (proj / "chats").is_dir()
    path.resolve().relative_to(home.resolve())
    assert ".agent" not in path.parts


def test_spark_home_override(tmp_path):
    other = tmp_path / "other-home"
    path = session_log_path_for(tmp_path, "s1", home=other)
    path.resolve().relative_to(other.resolve())
    assert path.name == "session.jsonl"
