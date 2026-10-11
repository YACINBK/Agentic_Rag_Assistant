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

        # On a retry pass, ACCUMULATE instead of replacing.
        #
        # The retry (node 05b) broadens the query and re-enters here. Overwriting
        # `retrieved_chunks` meant a broadened search returning worse candidates
        # silently discarded better ones from the first attempt — the gate then
        # judged the second pass alone and could decline a question the first pass
        # had already found evidence for. Nothing anywhere recovered the lost set.
        #
        # Union of the CANDIDATE pool, not of the reranked lists, and that
        # distinction is the whole point. Reranker scores are only comparable
        # within one query: attempt 1's chunks were scored against the original
        # rewritten query, attempt 2's against the broadened one. Merging two
        # scored lists would sort incomparable numbers against each other. Merging
        # the candidates instead sends the whole union through node 04 in one pass,
        # where every passage is scored against the same (broadened) query, so the
        # gate and the prune both see one coherent scale.
        #
        # Cost: the retry pass reranks up to 2x QDRANT_SEARCH_LIMIT passages. That
        # falls on the retry path only, which exists precisely because the first
        # answer was not good enough.
        previous = state.get("retrieved_chunks") or []
        if previous and state.get("retry_attempted"):
            seen = {chunk["chunk_id"] for chunk in chunks}
            chunks = chunks + [c for c in previous if c["chunk_id"] not in seen]

        return {**state, "retrieved_chunks": chunks}
