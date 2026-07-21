from __future__ import annotations

from app.core.settings import Settings
from app.pipeline.nodes.node_07_faithfulness import FaithfulnessNode
from tests.conftest import make_chunk, make_post_generation_state


class TestFaithfulnessNode:

    async def test_fully_grounded_answer_is_faithful(self) -> None:
        settings = Settings()
        node = FaithfulnessNode(settings=settings)
        chunks = [make_chunk(text="employees are entitled to 25 days of annual leave per year")]
        state = make_post_generation_state(
            reranked_chunks=chunks,
            generated_answer="Employees are entitled to 25 days of annual leave per year.",
        )

        result = await node.execute(state)

        assert result["is_faithful"] is True
        assert result["faithfulness_score"] > 0.5

    async def test_hallucinated_answer_is_not_faithful(self) -> None:
        settings = Settings()
        node = FaithfulnessNode(settings=settings)
        chunks = [make_chunk(text="the office is located in Paris France")]
        state = make_post_generation_state(
            reranked_chunks=chunks,
            generated_answer="Employees get unlimited vacation days and free lunch every day.",
        )

        result = await node.execute(state)

        assert result["is_faithful"] is False
        assert result["faithfulness_score"] < 0.5

    async def test_empty_answer_returns_unfaithful(self) -> None:
        settings = Settings()
        node = FaithfulnessNode(settings=settings)
        state = make_post_generation_state(generated_answer="")

        result = await node.execute(state)

        assert result["is_faithful"] is False
        assert result["faithfulness_score"] == 0.0

    async def test_empty_chunks_returns_unfaithful(self) -> None:
        settings = Settings()
        node = FaithfulnessNode(settings=settings)
        state = make_post_generation_state(
            reranked_chunks=[],
            generated_answer="Some answer text.",
        )

        result = await node.execute(state)

        assert result["is_faithful"] is False
        assert result["faithfulness_score"] == 0.0

    async def test_preserves_existing_state_keys(self) -> None:
        settings = Settings()
        node = FaithfulnessNode(settings=settings)
        state = make_post_generation_state()

        result = await node.execute(state)

        assert result["query"] == state["query"]
        assert result["generated_answer"] == state["generated_answer"]
        assert "is_faithful" in result
        assert "faithfulness_score" in result

    async def test_score_is_zero_when_no_overlap(self) -> None:
        settings = Settings()
        node = FaithfulnessNode(settings=settings)
        chunks = [make_chunk(text="abcdefghij klmnopqrst uvwxyz")]
        state = make_post_generation_state(
            reranked_chunks=chunks,
            generated_answer="The cat sat on the mat.",
        )

        result = await node.execute(state)

        assert result["faithfulness_score"] == 0.0
        assert result["is_faithful"] is False
