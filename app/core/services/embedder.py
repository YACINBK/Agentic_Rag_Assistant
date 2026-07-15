"""Abstract embedder service.

Concrete implementation uses BGE-M3 via Ollama.
BGE-M3 is the embedding model — do not substitute.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseEmbedder(ABC):

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per text."""
        ...

    @abstractmethod
    async def embed_single(self, text: str) -> list[float]:
        """Embed a single text. Convenience wrapper over embed()."""
        ...
