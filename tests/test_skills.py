"""Skill discovery and the short list in the system prompt."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from agent_loop.plugins.pack import official_plugin_dir, user_plugins_dir
from agent_loop.prompt.assemble import build_system_prompt
from agent_loop.prompt.skills import (
    bundled_skills_dir,
    discover_skills,
    ensure_user_skills,
    format_skills_prompt,
    user_skills_dir,
)


def _write_skill(root: Path, name: str, description: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nDo the thing.\n",
        encoding="utf-8",
    )
    return path


def test_update_copies_plugin_into_home(tmp_path, monkeypatch):
    from agent_loop.plugins.pack import ensure_user_plugins

    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path / "home"))
    ensure_user_plugins(tmp_path / "home")
    skills = discover_skills(tmp_path)
    bu = next(s for s in skills if s.name == "browser-use")
    home_skill = user_plugins_dir() / "browser-use" / "SKILL.md"
    assert bu.path == home_skill.resolve()
    assert "web_fetch" in bu.description
    assert (user_plugins_dir() / "browser-use" / "plugin.json").is_file()
    assert official_plugin_dir("browser-use") / "SKILL.md" != bu.path


def test_discover_does_not_overwrite_plugin_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path / "home"))
    dest = tmp_path / "home" / "plugins" / "browser-use"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(
        "---\nname: browser-use\ndescription: user kept this\n---\n\n# custom\n",
        encoding="utf-8",
    )
    bu = next(s for s in discover_skills(tmp_path) if s.name == "browser-use")
    assert bu.description == "user kept this"


def test_update_refreshes_official_plugin_files_and_keeps_the_rest(tmp_path, monkeypatch):
    from agent_loop.plugins.pack import ensure_user_plugins

    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    browser = home / "plugins" / "browser-use"
    browser.mkdir(parents=True)
    (browser / "SKILL.md").write_text(
        "---\nname: browser-use\ndescription: user kept this\n---\n\n# custom\n",
        encoding="utf-8",
    )
    (browser / "notes.txt").write_text("leave this\n", encoding="utf-8")
    computer = home / "plugins" / "computer-use"
    computer.mkdir(parents=True)
    (computer / "tools.md").write_text("# click\n\nkeep\n", encoding="utf-8")
    mine = home / "plugins" / "my-plugin"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text(
        "---\nname: my-plugin\ndescription: user plugin\n---\n\n# my-plugin\n",
        encoding="utf-8",
    )
    note = _write_skill(home / "skills", "notes", "user skill")

    before = discover_skills(tmp_path / "ws")
    assert next(s for s in before if s.name == "browser-use").description == "user kept this"
    ensure_user_plugins(home)
    skills = discover_skills(tmp_path / "ws")
    bu = next(s for s in skills if s.name == "browser-use")
    official = (official_plugin_dir("browser-use") / "SKILL.md").read_text(encoding="utf-8")
    assert bu.path == (browser / "SKILL.md").resolve()
    assert (browser / "SKILL.md").read_text(encoding="utf-8") == official
    assert (browser / "notes.txt").read_text(encoding="utf-8") == "leave this\n"
    assert (computer / "tools.md").read_text(encoding="utf-8") == "# click\n\nkeep\n"
    assert (computer / "SKILL.md").read_text(encoding="utf-8") == (
        official_plugin_dir("computer-use") / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert next(s for s in skills if s.name == "my-plugin").description == "user plugin"
    assert next(s for s in skills if s.name == "notes").path == note


def test_trading_skill_is_bundled_for_stock_and_crypto_questions():
    text = (bundled_skills_dir() / "stock-crypto-trading" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: stock-crypto-trading" in text
    assert "stock or cryptocurrency" in text
    assert "if-then" in text
    assert "战役" not in text


def test_official_pdf_skill_is_copied_and_refreshed(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    note = _write_skill(home / "skills", "notes", "user skill")
    bundled = bundled_skills_dir() / "pdf" / "SKILL.md"
    copied = home / "skills" / "pdf" / "SKILL.md"
    before = discover_skills(ws)
    assert all(s.name != "pdf" for s in before)
    assert not copied.exists()
    ensure_user_skills(home)
    skills = discover_skills(ws)
    pdf = next(s for s in skills if s.name == "pdf")
    assert pdf.path == copied.resolve()
    assert copied.read_text(encoding="utf-8") == bundled.read_text(encoding="utf-8")
    copied.write_text(
        "---\nname: pdf\ndescription: edited\n---\n\nold\n",
        encoding="utf-8",
    )
    ensure_user_skills(home)
    assert copied.read_text(encoding="utf-8") == bundled.read_text(encoding="utf-8")
    assert note.read_text(encoding="utf-8").startswith("---\nname: notes\n")


def test_project_skill_overrides_same_name(tmp_path):
    proj = tmp_path / ".permanent" / "skills"
    path = _write_skill(proj, "browser-use", "project override")
    skills = discover_skills(tmp_path)
    bu = next(s for s in skills if s.name == "browser-use")
    assert bu.description == "project override"
    assert bu.path == path


def test_official_plugin_skill_forbids_new_chrome():
    text = (official_plugin_dir("browser-use") / "SKILL.md").read_text(encoding="utf-8")
    assert "already running" in text
    assert "browser_exec" in text
    assert "browser_screenshot" in text
    assert "chrome://inspect/#remote-debugging" in text
    assert "Application Support/Google/Chrome" in text


def test_format_skills_prompt_lists_file_path(tmp_path):
    path = _write_skill(tmp_path, "demo", "A demo skill.")
    text = format_skills_prompt(discover_skills(tmp_path, roots=[tmp_path]))
    assert "# Skills" in text
    assert "read its SKILL.md" in text
    assert "- demo: A demo skill." in text
    assert f"File: {path.resolve().as_posix()}" in text


def test_assemble_includes_skills_between_notes_and_agents(tmp_path):
    from agent_loop.plugins.pack import ensure_user_plugins

    ensure_user_plugins()
    (tmp_path / "AGENTS.md").write_text("Use pytest.\n", encoding="utf-8")
    text = build_system_prompt(str(tmp_path), today=date(2026, 9, 10))
    assert "# Skills" in text
    assert "browser-use" in text
    home_skill = user_plugins_dir() / "browser-use" / "SKILL.md"
    assert f"File: {home_skill.resolve().as_posix()}" in text
    assert text.index("# Tool notes") < text.index("# Skills")
    assert text.index("# Skills") < text.index("# Project instructions (AGENTS.md)")
