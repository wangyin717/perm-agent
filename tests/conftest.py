"""测试不写真实 ~/.permanent。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_spark_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("spark-home")
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    return home
