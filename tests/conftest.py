"""测试不写真实 ~/.spark-agent。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_spark_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("spark-home")
    monkeypatch.setenv("SPARK_AGENT_HOME", str(home))
    return home
