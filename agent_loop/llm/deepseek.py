"""DeepSeek。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_loop.llm.client import ChatClient

PROVIDER_ID = "deepseek"
API_BASE = "https://api.deepseek.com"
ENV_NAME = "DEEPSEEK_API_KEY"
DEFAULT_MODEL = "deepseek-flash"
# 每一项是 (调用名, 菜单上的名字, 一行说明)。
MODEL_CHOICES = (
    ("deepseek-flash", "deepseek-flash", "Fast. Reads images."),
    ("deepseek-v4-pro", "deepseek-v4-pro", "Stronger. No images."),
)
# 文档上的旧名。deepseek-v4-flash 仍打到 Flash。
ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}
CONTEXT_WINDOWS = {
    "deepseek-flash": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-flash-vision-exp": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
}
# pro 不支持图像。旧 vision-exp 由 Flash 承接。
VISION_MODELS = {
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
}


class DeepSeek(ChatClient):
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
        payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tool_choice"] = "auto"


Client = DeepSeek
