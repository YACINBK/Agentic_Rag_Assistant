from __future__ import annotations

from app.core.settings import Settings
from app.pipeline.nodes.node_05b_retry import RetryNode
from tests.conftest import make_post_rerank_state


class TestRetryNode:

    async def test_first_retry_broadens_query_and_marks_attempted(self) -> None:
        settings = Settings()
        node = RetryNode(settings=settings)
        state = make_post_rerank_state(
            query="deployment process",
            rewritten_query="Whitecape deployment production release",
        )
        state.pop("retry_attempted", None)

        result = await node.execute(state)

        assert result["rewritten_query"] == "deployment process"
        assert result["retry_attempted"] is True
        assert result["retry_pass"] is True

    async def test_second_retry_is_blocked(self) -> None:
        settings = Settings()
        node = RetryNode(settings=settings)
        state = make_post_rerank_state(retry_attempted=True)

        result = await node.execute(state)

        assert result["retry_pass"] is False
        assert result["retry_attempted"] is True

    async def test_preserves_existing_state_keys(self) -> None:
        settings = Settings()
        node = RetryNode(settings=settings)
        state = make_post_rerank_state(
            query="original query",
            rewritten_query="fancy rewrite",
            classification="SIMPLE_RAG",
            user_role="developer",
        )
        state.pop("retry_attempted", None)

        result = await node.execute(state)

        assert result["query"] == "original query"
        assert result["classification"] == "SIMPLE_RAG"
        assert result["user_role"] == "developer"
        assert result["rewritten_query"] == "original query"
