"""Managed-install tag check and perm update guards."""
from __future__ import annotations

import subprocess
from pathlib import Path

from agent_loop.update import (
    is_managed_install,
    newer_than,
    newest_tag,
    parse_tag,
    peek_update,
    run_update,
)


def test_parse_and_newest_tag():
    assert parse_tag("v0.1.0") == (0, 1, 0)
    assert parse_tag("v0.1.10") == (0, 1, 10)
    assert parse_tag("main") is None
    assert newest_tag(["v0.1.9", "v0.1.10", "nightly"]) == "v0.1.10"
    assert newest_tag(["oops"]) is None


def test_newer_than():
    assert newer_than("v0.1.0", "v0.1.1")
    assert not newer_than("v0.2.0", "v0.1.9")
    assert newer_than(None, "v0.1.0")
    assert not newer_than("v0.1.0", None)


def test_dev_checkout_is_not_managed():
    assert is_managed_install() is False
    assert peek_update() is None


def test_run_update_without_src(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path / "home"))
    assert run_update() == 1


def test_install_sh_syntax():
    script = Path(__file__).resolve().parent.parent / "install.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
