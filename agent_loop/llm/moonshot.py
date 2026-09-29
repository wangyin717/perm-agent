"""Moonshot，模型名是 Kimi。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_loop.llm.client import ChatClient

PROVIDER_ID = "moonshot"
API_BASE = "https://api.moonshot.cn/v1"
ENV_NAME = "MOONSHOT_API_KEY"
DEFAULT_MODEL = "kimi-k3"
MODEL_CHOICES = (
    ("kimi-k3", "kimi-k3", "Kimi flagship. Reads images."),
    ("kimi-k2.7-code", "kimi-k2.7-code", "Kimi coding. Reads images."),
)
ALIASES: dict = {}
CONTEXT_WINDOWS = {
    "kimi-k3": 1_000_000,
    "kimi-k2.7-code": 256_000,
}
VISION_MODELS = {
    "kimi-k3",
    "kimi-k2.7-code",
}


class Moonshot(ChatClient):
    provider_id = PROVIDER_ID
    default_model = DEFAULT_MODEL
    api_base_default = API_BASE
    env_name = ENV_NAME

    def prepare_payload(
        self,
        payload: Dict[str, Any],
        tools: Optional[List[Dict]],
        kwargs: Dict[str, Any],
    ) -> None:
        # 不要传 temperature / top_p。Kimi 这些值是固定的，传了会报错。
        # K3 的思考关不掉，限长摘要用 low。K2.7 Code 只用默认思考，不传 reasoning_effort。
        if self.model == "kimi-k3":
            effort = kwargs.get("reasoning_effort")
            if effort is None and kwargs.get("max_tokens") is not None:
                effort = "low"
            payload["reasoning_effort"] = effort or "max"
        if tools:
            payload["tool_choice"] = "auto"


Client = Moonshot
