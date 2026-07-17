from __future__ import annotations

import litellm

from app.core.services.llm import BaseLLMService
from app.core.settings import settings


class LiteLLMService(BaseLLMService):

    def __init__(self) -> None:
        litellm.api_base = settings.OLLAMA_BASE_URL

    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
    ) -> str:
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=temperature,
            api_base=settings.OLLAMA_BASE_URL,
        )
        return response.choices[0].message.content
