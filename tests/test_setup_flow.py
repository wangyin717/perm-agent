"""第一次打开：选模型供应商，再可选填网页搜索密钥。"""
from __future__ import annotations

import asyncio
import os

from agent_loop.cli.setup import needs_setup, save_env_key
from agent_loop.envfile import _apply
from agent_loop.cli.tui import SparkTui


def test_load_dotenv_ignores_the_project_env(tmp_path, monkeypatch):
    import agent_loop.envfile as envfile

    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    (home / ".env").write_text(
        "DEEPSEEK_API_KEY=from-home\nZHIPU_API_KEY=\nPERPLEXITY_API_KEY=from-home-search\n",
        encoding="utf-8",
    )
    (project / ".env").write_text(
        "DEEPSEEK_API_KEY=from-project\nZHIPU_API_KEY=from-project-zhipu\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("PERMANENT_HOME", str(home))
    monkeypatch.setenv("PERPLEXITY_API_KEY", "already-in-process")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    envfile._loaded = False
    envfile.load_dotenv()
    assert os.environ["DEEPSEEK_API_KEY"] == "from-home"
    assert (os.environ.get("ZHIPU_API_KEY") or "") == ""
    assert os.environ["PERPLEXITY_API_KEY"] == "already-in-process"


def test_empty_env_assignment_does_not_block_a_later_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    first = tmp_path / "a.env"
    second = tmp_path / "b.env"
    first.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    second.write_text("DEEPSEEK_API_KEY=sk-from-home\n", encoding="utf-8")
    _apply(first)
    _apply(second)
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-from-home"


def test_pasted_twice_key_is_stored_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    save_env_key("DEEPSEEK_API_KEY", "sk-abcsk-abc")
    line = (tmp_path / ".env").read_text(encoding="utf-8").strip()
    assert line == "DEEPSEEK_API_KEY=sk-abc"


def test_model_menu_hides_providers_without_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)

    async def _run() -> None:
        app = SparkTui("model-ready", str(tmp_path / "ws"))
        async with app.run_test(size=(90, 24)) as pilot:
            await app._run_command("model", "/model")
            await pilot.pause()
            values = [row.value for row in app.query("#model-menu ModelOption")]
            assert values == ["deepseek-flash", "deepseek-v4-pro"]
            monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
            await app._run_command("model", "/model")
            await pilot.pause()
            values = [row.value for row in app.query("#model-menu ModelOption")]
            assert values == [
                "deepseek-flash",
                "deepseek-v4-pro",
                "kimi-k3",
                "kimi-k2.7-code",
            ]
            await app._run_command("login", "/login")
            await pilot.pause()
            labels = [row.value for row in app.query("#setup-menu ModelOption")]
            assert labels == ["model", "search"]

    asyncio.run(_run())


def test_setup_saves_provider_key_and_skips_search(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMANENT_HOME", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    assert needs_setup()

    async def _run() -> None:
        app = SparkTui("setup", str(tmp_path / "ws"))
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            assert app._setup_step == "category"
            labels = [row.value for row in app.query("#setup-menu ModelOption")]
            assert labels == ["model", "search"]
            app._choose_setup_category("model")
            await pilot.pause()
            rows = list(app.query("#setup-menu ProviderOption"))
            assert [row.value for row in rows] == ["deepseek", "glm", "kimi"]
            plains = [row.render().plain for row in rows]
            assert "deepseek-flash" not in " ".join(plains)
            assert "unconfigured" in plains[0]
            app._choose_setup_provider("kimi")
            await pilot.pause()
            assert app._setup_step == "apikey"
            note = str(app.query_one("#setup-note").content)
            assert "MOONSHOT_API_KEY" in note
            assert "Esc to go back" in note
            app.action_abort_turn()
            await pilot.pause()
            assert app._setup_step == "provider"
            app._choose_setup_provider("deepseek")
            await pilot.pause()
            assert app._setup_step == "apikey"
            assert "Esc to go back" in str(app.query_one("#setup-note").content)
            app.query_one("#prompt").value = "sk-test-deepseek"
            await pilot.press("enter")
            await pilot.pause()
            assert app._setup_step == "category"
            app._choose_setup_category("model")
            await pilot.pause()
            deepseek = app.query("#setup-menu ProviderOption")[0].render().plain
            assert "unconfigured" not in deepseek
            assert "configured" in deepseek
            app._show_setup_categories()
            await pilot.pause()
            app._choose_setup_category("search")
            await pilot.pause()
            note = str(app.query_one("#setup-note").content)
            assert "web search" in note
            assert "DuckDuckGo" in note
            assert "Esc to go back" in note
            await pilot.press("enter")
            await pilot.pause()
            assert app._setup_step == "category"
            app.action_abort_turn()
            await pilot.pause()
            assert app._setup_step == ""
            assert app._model_id == "deepseek-flash"

    asyncio.run(_run())
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=sk-test-deepseek" in env
    assert "PERPLEXITY_API_KEY" not in env
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-test-deepseek"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
