"""MiniMax。这里只接文本模型 MiniMax-M3。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_loop.llm.client import ChatClient

PROVIDER_ID = "minimax"
API_BASE = "https://api.minimax.cn/v1"
ENV_NAME = "MINIMAX_API_KEY"
DEFAULT_MODEL = "MiniMax-M3"
MODEL_CHOICES = (
    ("MiniMax-M3", "MiniMax-M3", "MiniMax. Reads images."),
)
ALIASES = {
    "minimax-m3": "MiniMax-M3",
}
CONTEXT_WINDOWS = {
    "MiniMax-M3": 1_000_000,
}
VISION_MODELS = {
    "minimax-m3",
}


class MiniMax(ChatClient):
    provider_id = PROVIDER_ID
    default_model = DEFAULT_MODEL
    api_base_default = API_BASE
    env_name = ENV_NAME
    # 下一轮要把 reasoning_details 原样带回，工具调用才接着上一轮的思考。
    keep_reasoning_details = True

    def prepare_payload(
        self,
        payload: Dict[str, Any],
        tools: Optional[List[Dict]],
        kwargs: Dict[str, Any],
    ) -> None:
        # 不传 temperature / top_p，用接口默认值。
        # reasoning_split 让思考走 reasoning_details，不混进正文。
        # 限长摘要如果还在思考，思考本身会占满 max_tokens，正文是空的。
        payload["stream_options"] = {"include_usage": True}
        payload["reasoning_split"] = True
        if kwargs.get("max_tokens") is not None:
            payload["thinking"] = {"type": "disabled"}
        if tools:
            payload["tool_choice"] = "auto"


Client = MiniMax
