from __future__ import annotations

import httpx

from app.core.services.reranker import BaseReranker
from app.core.settings import settings


class TEIReranker(BaseReranker):

    def __init__(self) -> None:
        self._base_url = settings.RERANKER_URL

    async def rerank(
        self,
        query: str,
        passages: list[str],
    ) -> list[float]:
        async with httpx.AsyncClient(timeout=settings.RERANKER_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{self._base_url}/rerank",
                json={"query": query, "texts": passages},
            )
            response.raise_for_status()
            results = response.json()
            return [item["score"] for item in sorted(results, key=lambda x: x["index"])]
