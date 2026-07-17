from __future__ import annotations

import httpx

from app.core.services.embedder import BaseEmbedder
from app.core.settings import settings


class OllamaEmbedder(BaseEmbedder):

    def __init__(self) -> None:
        self._base_url = settings.OLLAMA_BASE_URL
        self._model = settings.EMBEDDING_MODEL

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            for text in texts:
                response = await client.post(
                    f"{self._base_url}/api/embed",
                    json={"model": self._model, "input": text},
                )
                response.raise_for_status()
                vectors.append(response.json()["embeddings"][0])
        return vectors

    async def embed_single(self, text: str) -> list[float]:
        result = await self.embed([text])
        return result[0]
