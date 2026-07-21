from __future__ import annotations

from app.core.settings import Settings
from app.pipeline.nodes.node_05_relevance_gate import RelevanceGateNode
from tests.conftest import make_chunk, make_post_rerank_state


class TestRelevanceGateNode:

    async def test_pass_when_top_score_above_threshold(self) -> None:
        settings = Settings(RELEVANCE_THRESHOLD=0.5)
        node = RelevanceGateNode(settings=settings)
        state = make_post_rerank_state()

        result = await node.execute(state)

        assert result["relevance_pass"] is True

    async def test_fail_when_top_score_below_threshold(self) -> None:
        settings = Settings(RELEVANCE_THRESHOLD=0.5)
        node = RelevanceGateNode(settings=settings)
        chunks = [
            make_chunk(score=0.3),
            make_chunk(score=0.2),
        ]
        state = make_post_rerank_state(reranked_chunks=chunks)

        result = await node.execute(state)

        assert result["relevance_pass"] is False

    async def test_fail_when_chunks_empty(self) -> None:
        settings = Settings()
        node = RelevanceGateNode(settings=settings)
        state = make_post_rerank_state(reranked_chunks=[])

        result = await node.execute(state)

        assert result["relevance_pass"] is False

    async def test_pass_at_exact_threshold(self) -> None:
        settings = Settings(RELEVANCE_THRESHOLD=0.5)
        node = RelevanceGateNode(settings=settings)
        chunks = [make_chunk(score=0.5)]
        state = make_post_rerank_state(reranked_chunks=chunks)

        result = await node.execute(state)

        assert result["relevance_pass"] is True

    async def test_preserves_existing_state_keys(self) -> None:
        settings = Settings()
        node = RelevanceGateNode(settings=settings)
        state = make_post_rerank_state()

        result = await node.execute(state)

        assert result["query"] == state["query"]
        assert result["rewritten_query"] == state["rewritten_query"]
        assert result["reranked_chunks"] is state["reranked_chunks"]
        assert "relevance_pass" in result
