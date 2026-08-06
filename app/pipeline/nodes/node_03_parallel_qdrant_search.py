from __future__ import annotations

from app.core.base_node import BaseNode
from app.core.exceptions import RetrievalError
from app.core.services.embedder import BaseEmbedder
from app.core.services.vector_store import BaseVectorStore
from app.core.settings import Settings
from app.core.state import PipelineState


class QdrantSearchNode(BaseNode):
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
        return "qdrant_search"

    async def execute(self, state: PipelineState) -> PipelineState:
        rewritten = state.get("rewritten_query", "").strip()
        if not rewritten:
            raise RetrievalError("rewritten_query is missing or empty")

        user_role = state["user_role"]
        user_is_admin = state["user_is_admin"]
        allowed_roles = [user_role, "all"]

        try:
            vector = await self._embedder.embed_single(rewritten)
        except Exception as e:
            raise RetrievalError(f"Embedding failed: {e}") from e

        try:
            chunks = await self._vector_store.search(
                collection=self._settings.QDRANT_COLLECTION,
                query_vector=vector,
                allowed_roles=allowed_roles,
                user_is_admin=user_is_admin,
                limit=self._settings.QDRANT_SEARCH_LIMIT,
            )
        except Exception as e:
            raise RetrievalError(f"Vector search failed: {e}") from e

        return {**state, "retrieved_chunks": chunks}
