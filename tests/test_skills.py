"""Skill discovery and the short list in the system prompt."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from agent_loop.plugins.pack import official_plugin_dir, user_plugins_dir
from agent_loop.prompt.assemble import build_system_prompt
from agent_loop.prompt.skills import (
    discover_skills,
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


def test_discover_copies_plugin_into_home(tmp_path):
    skills = discover_skills(tmp_path)
    names = [s.name for s in skills]
    assert "browser-use" in names
    bu = next(s for s in skills if s.name == "browser-use")
    assert "web_fetch" in bu.description
    home_skill = user_plugins_dir() / "browser-use" / "SKILL.md"
    assert bu.path == home_skill.resolve()
    assert home_skill.is_file()
    assert (user_plugins_dir() / "browser-use" / "plugin.json").is_file()
    assert official_plugin_dir("browser-use") / "SKILL.md" != bu.path


def test_replaces_factory_stub(tmp_path):
    dest = user_plugins_dir() / "browser-use"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(
        "---\nname: browser-use\ndescription: old\n---\n\n# browser-use (placeholder)\n",
        encoding="utf-8",
    )
    bu = next(s for s in discover_skills(tmp_path) if s.name == "browser-use")
    text = bu.path.read_text(encoding="utf-8")
    assert "web_fetch" in bu.description
    assert "(placeholder)" not in text
    assert "never print" in text.lower()


def test_does_not_overwrite_user_plugin_skill(tmp_path):
    dest = user_plugins_dir() / "browser-use"
    dest.mkdir(parents=True)
    custom = dest / "SKILL.md"
    custom.write_text(
        "---\nname: browser-use\ndescription: user kept this\n---\n\n# custom\n",
        encoding="utf-8",
    )
    bu = next(s for s in discover_skills(tmp_path) if s.name == "browser-use")
    assert bu.description == "user kept this"
    assert "user kept this" in custom.read_text(encoding="utf-8")


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
    (tmp_path / "AGENTS.md").write_text("Use pytest.\n", encoding="utf-8")
    text = build_system_prompt(str(tmp_path), today=date(2026, 9, 10))
    assert "# Skills" in text
    assert "browser-use" in text
    home_skill = user_plugins_dir() / "browser-use" / "SKILL.md"
    assert f"File: {home_skill.resolve().as_posix()}" in text
    assert text.index("# Tool notes") < text.index("# Skills")
    assert text.index("# Skills") < text.index("# Project instructions (AGENTS.md)")
