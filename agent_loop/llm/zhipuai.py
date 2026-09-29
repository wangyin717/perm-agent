"""智谱。模型名是 GLM。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_loop.llm.client import ChatClient

PROVIDER_ID = "zhipuai"
API_BASE = "https://open.bigmodel.cn/api/paas/v4"
ENV_NAME = "ZHIPU_API_KEY"
DEFAULT_MODEL = "glm-5.3-flash"
MODEL_CHOICES = (
    ("glm-5.3-flash", "glm-5.3-flash", "Zhipu. Reads images."),
    ("glm-5.3", "glm-5.3", "Zhipu flagship. No images."),
)
ALIASES: dict = {}
CONTEXT_WINDOWS = {
    "glm-5.3-flash": 1_000_000,
    "glm-5.3": 1_000_000,
}
VISION_MODELS = {
    "glm-5.3-flash",
}


class ZhipuAI(ChatClient):
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
        # GLM-5.3 不能关思考。不传 temperature 和 top_p，用接口默认值。
        # 记忆整理这种限长请求用 low，避免正文已经结束又卡一轮深度思考。
        payload["thinking"] = {"type": "enabled"}
        effort = kwargs.get("reasoning_effort")
        if effort is None and kwargs.get("max_tokens") is not None:
            effort = "low"
        payload["reasoning_effort"] = effort or "max"
        if tools:
            payload["tool_stream"] = True


Client = ZhipuAI
