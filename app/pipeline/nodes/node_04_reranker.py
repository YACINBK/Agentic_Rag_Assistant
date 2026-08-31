from __future__ import annotations

from app.core.base_node import BaseNode
from app.core.exceptions import RetrievalError
from app.core.services.reranker import BaseReranker
from app.core.settings import Settings
from app.core.state import ChunkPayload, PipelineState


class RerankerNode(BaseNode):
    def __init__(self, reranker: BaseReranker, settings: Settings) -> None:
        self._reranker = reranker
        self._settings = settings

    @property
    def name(self) -> str:
        return "reranker"

    async def execute(self, state: PipelineState) -> PipelineState:
        rewritten = state.get("rewritten_query", "").strip()
        if not rewritten:
            raise RetrievalError("rewritten_query is missing or empty")

        chunks: list[ChunkPayload] = state.get("retrieved_chunks", [])
        if not chunks:
            return {**state, "reranked_chunks": []}

        passages = [c["text"] for c in chunks]
        try:
            scores = await self._reranker.rerank(query=rewritten, passages=passages)
        except Exception as e:
            # type(e).__name__ is load-bearing: httpx timeout exceptions stringify to
            # '', so a bare {e} produced "Reranker failed: " and said nothing about
            # whether TEI refused, was unreachable, or simply ran past the ceiling.
            raise RetrievalError(f"Reranker failed: {type(e).__name__}: {e}") from e

        if len(scores) != len(chunks):
            raise RetrievalError(
                f"Reranker returned {len(scores)} scores for {len(chunks)} passages"
            )

        reranked: list[ChunkPayload] = []
        for chunk, score in zip(chunks, scores):
            reranked.append({**chunk, "score": score})

        reranked.sort(key=lambda c: c["score"], reverse=True)

        return {**state, "reranked_chunks": reranked}
