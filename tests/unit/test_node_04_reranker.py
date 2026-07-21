from __future__ import annotations

import pytest

from app.core.exceptions import RetrievalError
from app.core.settings import Settings
from app.pipeline.nodes.node_04_reranker import RerankerNode
from tests.conftest import MockReranker, make_post_retrieval_state


class TestRerankerNode:

    async def test_reranks_and_sorts_by_score(self) -> None:
        reranker = MockReranker(scores=[0.3, 0.9, 0.5])
        settings = Settings()
        node = RerankerNode(reranker=reranker, settings=settings)
        state = make_post_retrieval_state(num_chunks=3)

        result = await node.execute(state)

        scores = [c["score"] for c in result["reranked_chunks"]]
        assert scores == [0.9, 0.5, 0.3]
        assert len(reranker.calls) == 1

    async def test_empty_chunks_skips_reranker(self) -> None:
        reranker = MockReranker()
        settings = Settings()
        node = RerankerNode(reranker=reranker, settings=settings)
        state = make_post_retrieval_state(
            num_chunks=0,
            retrieved_chunks=[],
        )

        result = await node.execute(state)

        assert result["reranked_chunks"] == []
        assert len(reranker.calls) == 0

    async def test_missing_rewritten_query_raises_error(self) -> None:
        reranker = MockReranker()
        settings = Settings()
        node = RerankerNode(reranker=reranker, settings=settings)
        state = make_post_retrieval_state(num_chunks=2)
        state.pop("rewritten_query", None)

        with pytest.raises(RetrievalError):
            await node.execute(state)

    async def test_preserves_existing_state_keys(self) -> None:
        reranker = MockReranker(scores=[0.8, 0.6])
        settings = Settings()
        node = RerankerNode(reranker=reranker, settings=settings)
        state = make_post_retrieval_state(num_chunks=2)

        result = await node.execute(state)

        assert result["query"] == state["query"]
        assert result["rewritten_query"] == state["rewritten_query"]
        assert result["classification"] == state["classification"]
        assert result["retrieved_chunks"] is state["retrieved_chunks"]
        assert len(result["reranked_chunks"]) == 2

    async def test_reranker_called_with_correct_inputs(self) -> None:
        reranker = MockReranker(scores=[0.7, 0.4])
        settings = Settings()
        node = RerankerNode(reranker=reranker, settings=settings)
        state = make_post_retrieval_state(
            num_chunks=2,
            rewritten_query="specific query text",
        )

        await node.execute(state)

        assert reranker.calls[0]["query"] == "specific query text"
        assert len(reranker.calls[0]["passages"]) == 2
