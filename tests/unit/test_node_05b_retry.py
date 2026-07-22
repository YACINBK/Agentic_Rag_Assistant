from __future__ import annotations

from app.core.settings import Settings
from app.pipeline.nodes.node_05b_retry import RetryNode
from tests.conftest import MockLLMService, make_post_rerank_state


class TestRetryNode:

    async def test_first_retry_broadens_query_via_llm(self) -> None:
        llm = MockLLMService(response="deployment release process overview")
        settings = Settings()
        node = RetryNode(llm=llm, settings=settings)
        state = make_post_rerank_state(
            query="deployment process",
            rewritten_query="Whitecape deployment production release",
        )
        state.pop("retry_attempted", None)

        result = await node.execute(state)

        assert result["rewritten_query"] == "deployment release process overview"
        assert result["retry_attempted"] is True
        assert len(llm.calls) == 1
        assert llm.calls[0]["model"] == settings.REWRITER_MODEL

    async def test_first_retry_falls_back_to_original_on_llm_failure(self) -> None:
        llm = MockLLMService(response="irrelevant")

        async def failing_complete(model, messages, temperature=0.0):
            llm.calls.append({"model": model, "messages": messages, "temperature": temperature})
            raise RuntimeError("Ollama down")

        llm.complete = failing_complete
        settings = Settings()
        node = RetryNode(llm=llm, settings=settings)
        state = make_post_rerank_state(
            query="deployment process",
            rewritten_query="Whitecape deployment production release",
        )
        state.pop("retry_attempted", None)

        result = await node.execute(state)

        assert result["rewritten_query"] == "deployment process"
        assert result["retry_attempted"] is True

    async def test_second_retry_is_blocked(self) -> None:
        llm = MockLLMService(response="should not be called")
        settings = Settings()
        node = RetryNode(llm=llm, settings=settings)
        state = make_post_rerank_state(retry_attempted=True)

        result = await node.execute(state)

        assert result["retry_attempted"] is True
        assert len(llm.calls) == 0

    async def test_preserves_existing_state_keys(self) -> None:
        llm = MockLLMService(response="broader query")
        settings = Settings()
        node = RetryNode(llm=llm, settings=settings)
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
        assert result["rewritten_query"] == "broader query"

    async def test_empty_llm_response_falls_back_to_original(self) -> None:
        llm = MockLLMService(response="   ")
        settings = Settings()
        node = RetryNode(llm=llm, settings=settings)
        state = make_post_rerank_state(
            query="the original",
            rewritten_query="too narrow",
        )
        state.pop("retry_attempted", None)

        result = await node.execute(state)

        assert result["rewritten_query"] == "the original"
        assert result["retry_attempted"] is True
