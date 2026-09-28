"""第一次打开时配置模型密钥。"""

from __future__ import annotations

import os
from pathlib import Path

from agent_loop.paths import spark_home

# id, 菜单上的名字, 说明, 默认模型。
PROVIDERS = (
    ("deepseek", "DeepSeek", "", "deepseek-flash"),
    ("glm", "GLM", "", "glm-5.3-flash"),
    ("kimi", "Kimi", "", "kimi-k3"),
)
PROVIDER_ENV = {
    "deepseek": "DEEPSEEK_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
}
SEARCH_ENV = "PERPLEXITY_API_KEY"
CATEGORIES = (
    ("model", "Model", "model API key"),
    ("search", "Web search", "PERPLEXITY_API_KEY"),
)
SEARCH_NOTE = (
    "PERPLEXITY_API_KEY is used for web search. "
    "Press Enter to skip and use the default search (DuckDuckGo). "
    "Esc to go back."
)


def configured_model_choices():
    """只返回已经写了 API 密钥的供应商下的模型。"""
    from agent_loop.llm.deepseek import MODEL_CHOICES, provider_of

    ready = []
    for choice in MODEL_CHOICES:
        provider = {"zhipu": "glm"}.get(provider_of(choice[0]), provider_of(choice[0]))
        env_name = PROVIDER_ENV.get(provider)
        if env_name and (os.environ.get(env_name) or "").strip():
            ready.append(choice)
    return ready


def needs_setup() -> bool:
    for env_name in PROVIDER_ENV.values():
        if (os.environ.get(env_name) or "").strip():
            return False
    return True


def provider_configured(provider: str) -> bool:
    env_name = PROVIDER_ENV.get(provider)
    return bool(env_name and (os.environ.get(env_name) or "").strip())


def default_model(provider: str) -> str:
    for pid, _label, _hint, model in PROVIDERS:
        if pid == provider:
            return model
    return ""


def _undouble(value: str) -> str:
    # 输入框里同一串密钥粘了两次时，两半完全一样。
    if len(value) >= 8 and len(value) % 2 == 0:
        half = len(value) // 2
        if value[:half] == value[half:]:
            return value[:half]
    return value


def save_env_key(name: str, value: str) -> None:
    value = _undouble((value or "").strip())
    if not name or not value or "\n" in value or "\r" in value:
        raise ValueError("empty key")
    path = spark_home() / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    found = False
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped.startswith(f"{name}=") or stripped.startswith(f"export {name}="):
                lines.append(f"{name}={value}")
                found = True
            else:
                lines.append(raw)
    if not found:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    os.environ[name] = value
