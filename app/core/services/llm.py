"""Abstract LLM service. All LLM calls go through this interface.

Concrete implementation uses LiteLLM → Ollama.
No node imports anthropic or openai directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseLLMService(ABC):

    @abstractmethod
    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
    ) -> str:
        """Send a chat completion request, return the response text."""
        ...
