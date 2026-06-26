"""
Provider cascade: DeepSeek → DeepInfra → OpenRouter → rule-based fallback.
The first provider with a valid API key and a successful response wins.
"""
import os
import logging
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


@dataclass
class Provider:
    name: str
    base_url: str
    api_key_env: str
    model: str
    extra_headers: Optional[dict] = None

    @property
    def api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def make_client(self) -> AsyncOpenAI:
        headers = self.extra_headers or {}
        return AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=8.0,
            default_headers=headers,
        )


# Ordered by preference: fastest/best first
PROVIDERS = [
    Provider(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        model="deepseek-chat",
    ),
    Provider(
        name="deepinfra",
        base_url="https://api.deepinfra.com/v1/openai",
        api_key_env="DEEPINFRA_API_KEY",
        model="Qwen/Qwen2.5-72B-Instruct",
    ),
    Provider(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        model="nvidia/nemotron-3-super-120b-a12b:free",
        extra_headers={
            "HTTP-Referer": "https://github.com/queuestorm-investigator",
            "X-Title": "QueueStorm Investigator",
        },
    ),
]


def get_active_providers() -> list[Provider]:
    active = [p for p in PROVIDERS if p.available]
    if not active:
        logger.warning("No LLM provider API keys found — will use rule-based fallback only")
    else:
        logger.info(f"Active providers: {[p.name for p in active]}")
    return active
