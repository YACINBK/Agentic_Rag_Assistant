from __future__ import annotations

import litellm

from app.core.services.llm import BaseLLMService
from app.core.settings import settings


class LiteLLMService(BaseLLMService):
    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
    ) -> str:
        # api_base is scoped to Ollama models: it is the local runtime's address,
        # and pinning it globally would hijack every other provider (OpenRouter,
        # etc.) into pointing at localhost. Non-Ollama models use LiteLLM's own
        # provider routing, which already knows their endpoints.
        call_kwargs: dict[str, object] = {
            # A call with no ceiling can wait forever on a stalled request.
            # Applied here, in the single place every LLM call funnels through (§4),
            # rather than per node — no caller can forget it.
            "timeout": settings.LLM_TIMEOUT_SECONDS,
        }
        if model.startswith("ollama/"):
            call_kwargs["api_base"] = settings.OLLAMA_BASE_URL

        response = await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=temperature,
            **call_kwargs,
        )
        return response.choices[0].message.content
