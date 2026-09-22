"""Managed-install tag check and perm update guards."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agent_loop.update import (
    is_managed_install,
    migrate_uv_tools_dir,
    newer_than,
    newest_tag,
    parse_tag,
    peek_update,
    run_update,
    write_wrappers,
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


def test_write_wrappers_put_home_bin_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    (home / "src" / ".venv" / "bin").mkdir(parents=True)
    (home / "src" / ".venv" / "bin" / "perm").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("agent_loop.update.Path.home", lambda: tmp_path)
    write_wrappers(home)
    text = (tmp_path / ".local" / "bin" / "perm").read_text(encoding="utf-8")
    assert f'PATH="{home / "bin"}:$PATH"' in text
    assert f'UV_TOOL_DIR="{home / "uv-tools"}"' in text


def test_migrate_uv_tools_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    old = tmp_path / "tools"
    old.mkdir()
    (old / "marker").write_text("ok", encoding="utf-8")
    dest = migrate_uv_tools_dir(tmp_path)
    assert dest == tmp_path / "uv-tools"
    assert dest.is_dir()
    assert (dest / "marker").read_text(encoding="utf-8") == "ok"
    assert not old.exists()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (dest / "browser-use").mkdir()
    (dest / "browser-use" / "run").write_text("x", encoding="utf-8")
    (bindir / "browser-use").symlink_to(str(old / "browser-use" / "run"))
    (dest / "shim").write_text(f"#!{old}/browser-use/python\n", encoding="utf-8")
    migrate_uv_tools_dir(tmp_path)
    assert os.readlink(bindir / "browser-use") == str(dest / "browser-use" / "run")
    assert str(dest) in (dest / "shim").read_text(encoding="utf-8")
    assert migrate_uv_tools_dir(tmp_path) == dest


def test_install_sh_syntax():
    script = Path(__file__).resolve().parent.parent / "install.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
