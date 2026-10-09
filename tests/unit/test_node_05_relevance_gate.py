from __future__ import annotations

import pytest
from pydantic import ValidationError

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

    async def test_prunes_only_after_gate_passes(self) -> None:
        settings = Settings(CONTEXT_PRUNE_THRESHOLD=0.1)
        chunks = [
            make_chunk(score=0.95), make_chunk(score=0.29),
            make_chunk(score=0.1), make_chunk(score=0.00075),
        ]
        state = make_post_rerank_state(reranked_chunks=chunks)

        result = await RelevanceGateNode(settings).execute(state)

        assert result["relevance_pass"] is True
        assert result["reranked_chunks"] == chunks[:3]
        assert all(
            retained is original
            for retained, original in zip(result["reranked_chunks"], chunks)
        )
        assert state["reranked_chunks"] is chunks
        assert len(chunks) == 4

    async def test_failed_gate_keeps_evidence_for_retry(self) -> None:
        # Gate pinned explicitly: this test is about what happens BELOW the gate,
        # so it must not inherit whatever the current default happens to be.
        settings = Settings(RELEVANCE_THRESHOLD=0.5, CONTEXT_PRUNE_THRESHOLD=0.1)
        chunks = [make_chunk(score=0.29), make_chunk(score=0.01)]
        state = make_post_rerank_state(reranked_chunks=chunks)

        result = await RelevanceGateNode(settings).execute(state)

        assert result["relevance_pass"] is False
        assert result["reranked_chunks"] is chunks

    async def test_one_survivor_is_not_padded_with_noise(self) -> None:
        chunks = [make_chunk(score=0.5), make_chunk(score=0.001)]
        result = await RelevanceGateNode(Settings(CONTEXT_PRUNE_THRESHOLD=0.1)).execute(
            make_post_rerank_state(reranked_chunks=chunks)
        )

        assert result["relevance_pass"] is True
        assert result["reranked_chunks"] == [chunks[0]]

    async def test_zero_threshold_preserves_original_candidates(self) -> None:
        chunks = [make_chunk(score=0.9), make_chunk(score=0.0)]
        result = await RelevanceGateNode(Settings(CONTEXT_PRUNE_THRESHOLD=0.0)).execute(
            make_post_rerank_state(reranked_chunks=chunks)
        )

        assert result["relevance_pass"] is True
        assert result["reranked_chunks"] is chunks

    async def test_pruning_preserves_citation_metadata(self) -> None:
        retained = make_chunk(
            score=0.9, section_path="Commandes > Copier", anchor="bkmrk-copy",
            image_refs=[{"image_id": "image-1", "anchor": "bkmrk-copy"}],
        )
        result = await RelevanceGateNode(Settings(CONTEXT_PRUNE_THRESHOLD=0.1)).execute(
            make_post_rerank_state(reranked_chunks=[retained, make_chunk(score=0.01)])
        )

        assert result["reranked_chunks"] == [retained]
        assert result["reranked_chunks"][0] is retained

    @pytest.mark.parametrize("threshold", [-0.01, 1.01, float("nan"), float("inf"), 0.51])
    def test_invalid_pruning_threshold_is_rejected(self, threshold: float) -> None:
        with pytest.raises(ValidationError):
            Settings(RELEVANCE_THRESHOLD=0.5, CONTEXT_PRUNE_THRESHOLD=threshold)
