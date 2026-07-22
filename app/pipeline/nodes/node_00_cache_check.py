from __future__ import annotations

from app.core.base_node import BaseNode
from app.core.services.embedder import BaseEmbedder
from app.core.services.vector_store import BaseVectorStore
from app.core.settings import Settings
from app.core.state import PipelineState


class CacheCheckNode(BaseNode):
    def __init__(
        self,
        embedder: BaseEmbedder,
        vector_store: BaseVectorStore,
        settings: Settings,
    ) -> None:
        self._embedder = embedder
        self._vector_store = vector_store
        self._settings = settings

    @property
    def name(self) -> str:
        return "cache_check"

    async def execute(self, state: PipelineState) -> PipelineState:
        query = state["query"]
        user_role = state["user_role"]

        try:
            vector = await self._embedder.embed_single(query)
        except Exception:
            return {**state, "cache_hit": False}

        try:
            results = await self._vector_store.search(
                collection=self._settings.QDRANT_CACHE_COLLECTION,
                query_vector=vector,
                allowed_roles=[user_role],
                limit=1,
            )
        except Exception:
            return {**state, "cache_hit": False}

        if results and results[0]["score"] >= self._settings.CACHE_SIMILARITY_THRESHOLD:
            return {
                **state,
                "cache_hit": True,
                "cached_answer": results[0]["text"],
            }

        return {**state, "cache_hit": False}
