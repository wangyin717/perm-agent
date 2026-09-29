"""把三家的模型名单收成一份，并按模型名选出该用哪一家。"""

from __future__ import annotations

import os
from typing import Optional

import agent_loop.llm.deepseek as deepseek
import agent_loop.llm.moonshot as moonshot
import agent_loop.llm.zhipuai as zhipuai

DEFAULT_CONTEXT_WINDOW = 1_000_000
DEFAULT_MODEL = "deepseek-flash"

_MODULES = (deepseek, zhipuai, moonshot)
MODEL_CHOICES = tuple(item for mod in _MODULES for item in mod.MODEL_CHOICES)
_ALIASES: dict = {}
CONTEXT_WINDOWS: dict = {}
_VISION: set = set()
for _mod in _MODULES:
    _ALIASES.update(_mod.ALIASES)
    CONTEXT_WINDOWS.update(_mod.CONTEXT_WINDOWS)
    _VISION.update(_mod.VISION_MODELS)


def canonical_model(name: Optional[str]) -> str:
    raw = (name or "").strip()
    mapped = _ALIASES.get(raw, raw)
    known = {model_id for model_id, _label, _description in MODEL_CHOICES}
    if mapped in known:
        return mapped
    return DEFAULT_MODEL


def module_for(model_id: str):
    raw = (model_id or "").strip()
    mapped = _ALIASES.get(raw, raw)
    known = {model_id for model_id, _label, _description in MODEL_CHOICES}
    if mapped not in known:
        mapped = DEFAULT_MODEL
    for mod in _MODULES:
        if mapped in {item[0] for item in mod.MODEL_CHOICES}:
            return mod
    return _MODULES[0]


def provider_of(model_id: str) -> str:
    return module_for(model_id).PROVIDER_ID


def context_window_for(model_id: str) -> int:
    return CONTEXT_WINDOWS.get(model_id, DEFAULT_CONTEXT_WINDOW)


def is_vision(model_id: str) -> bool:
    return (model_id or "").strip().lower() in _VISION


def api_key_for(model_id: str) -> str:
    mod = module_for(model_id)
    key = (os.environ.get(mod.ENV_NAME) or "").strip()
    if not key:
        raise RuntimeError(f"{mod.ENV_NAME} is not set (put it in ~/.permanent/.env)")
    return key


def configured_model() -> str:
    from agent_loop.plugins.config import load_config

    raw = load_config().get("model")
    if isinstance(raw, str):
        return canonical_model(raw)
    return DEFAULT_MODEL


def save_model(model_id: str) -> str:
    from agent_loop.plugins.config import set_setting

    model_id = canonical_model(model_id)
    set_setting("model", model_id)
    return model_id


def make_client(
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    timeout: Optional[float] = None,
):
    """按模型名构造对应那一家的客户端。"""
    chosen = canonical_model(model) if model else configured_model()
    mod = module_for(chosen)
    kwargs = {"model": chosen, "api_key": api_key, "api_base": api_base}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return mod.Client(**kwargs)
