"""Abstract reranker service.

Concrete implementation calls bge-reranker-v2-m3 via Hugging Face TEI.
NOT Ollama — Ollama cannot run cross-encoder architectures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseReranker(ABC):

    @abstractmethod
    async def rerank(
        self,
        query: str,
        passages: list[str],
    ) -> list[float]:
        """Score (query, passage) pairs jointly. Returns one score per passage."""
        ...
