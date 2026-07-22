from __future__ import annotations

from app.core.settings import Settings
from app.pipeline.nodes.node_02_rewriter_decomposer import RewriterNode
from tests.conftest import MockLLMService, make_state


class TestRewriterNode:

    async def test_rewrites_query_for_retrieval(self) -> None:
        mock_llm = MockLLMService(response="Whitecape deployment process steps")
        settings = Settings()
        node = RewriterNode(llm=mock_llm, settings=settings)
        state = make_state(query="How do we deploy?", classification="SIMPLE_RAG")

        result = await node.execute(state)

        assert isinstance(result["rewritten_query"], str)
        assert len(result["rewritten_query"]) > 0
        assert result["rewritten_query"] != state["query"]
        assert result["sub_queries"] == []
        assert len(mock_llm.calls) == 1

    async def test_direct_classification_skips_rewrite(self) -> None:
        mock_llm = MockLLMService(response="anything")
        settings = Settings()
        node = RewriterNode(llm=mock_llm, settings=settings)
        state = make_state(query="hello", classification="DIRECT")

        result = await node.execute(state)

        assert "rewritten_query" not in result
        assert "sub_queries" not in result
        assert len(mock_llm.calls) == 0

    async def test_llm_failure_falls_back_to_original(self) -> None:
        mock_llm = MockLLMService(response="irrelevant")

        async def failing_complete(model, messages, temperature=0.0):
            mock_llm.calls.append({"model": model, "messages": messages, "temperature": temperature})
            raise RuntimeError("Ollama down")

        mock_llm.complete = failing_complete
        settings = Settings()
        node = RewriterNode(llm=mock_llm, settings=settings)
        state = make_state(query="How do we deploy?", classification="SIMPLE_RAG")

        result = await node.execute(state)

        assert result["rewritten_query"] == state["query"]
        assert result["sub_queries"] == []

    async def test_empty_llm_response_falls_back(self) -> None:
        mock_llm = MockLLMService(response="   ")
        settings = Settings()
        node = RewriterNode(llm=mock_llm, settings=settings)
        state = make_state(query="How do we deploy?", classification="SIMPLE_RAG")

        result = await node.execute(state)

        assert result["rewritten_query"] == state["query"]
        assert result["sub_queries"] == []

    async def test_preserves_existing_state_keys(self) -> None:
        mock_llm = MockLLMService(response="rewritten version")
        settings = Settings()
        node = RewriterNode(llm=mock_llm, settings=settings)
        state = make_state(
            query="What is the leave policy?",
            classification="SIMPLE_RAG",
            user_role="developer",
        )

        result = await node.execute(state)

        assert result["query"] == state["query"]
        assert result["classification"] == "SIMPLE_RAG"
        assert result["user_role"] == "developer"
        assert result["rewritten_query"] == "rewritten version"
        assert result["sub_queries"] == []
