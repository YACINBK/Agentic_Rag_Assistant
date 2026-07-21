from __future__ import annotations

import pytest

from app.core.exceptions import GenerationError
from app.core.settings import Settings
from app.pipeline.nodes.node_06_generator import GeneratorNode
from tests.conftest import MockLLMService, make_post_rerank_state


class TestGeneratorNode:

    async def test_generates_cited_answer_from_chunks(self) -> None:
        llm = MockLLMService(response="According to company policy, leave is 25 days [source: handbook.pdf].")
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(num_chunks=2)

        result = await node.execute(state)

        assert isinstance(result["generated_answer"], str)
        assert len(result["generated_answer"]) > 0
        assert len(llm.calls) == 1
        assert llm.calls[0]["model"] == settings.GENERATOR_MODEL

    async def test_empty_chunks_produces_fallback_answer(self) -> None:
        llm = MockLLMService()
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(reranked_chunks=[])

        result = await node.execute(state)

        assert isinstance(result["generated_answer"], str)
        assert len(result["generated_answer"]) > 0
        assert len(llm.calls) == 0

    async def test_llm_failure_raises_generation_error(self) -> None:
        llm = MockLLMService(response="irrelevant")

        async def failing_complete(model, messages, temperature=0.0):
            llm.calls.append({"model": model, "messages": messages, "temperature": temperature})
            raise RuntimeError("Ollama down")

        llm.complete = failing_complete
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(num_chunks=1)

        with pytest.raises(GenerationError):
            await node.execute(state)

    async def test_prompt_includes_role_and_chunks(self) -> None:
        llm = MockLLMService(response="Answer text.")
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(num_chunks=1, user_role="qa_engineer")

        await node.execute(state)

        system_msg = llm.calls[0]["messages"][0]["content"]
        user_msg = llm.calls[0]["messages"][1]["content"]
        assert "qa_engineer" in system_msg
        assert state["query"] in user_msg
        assert "Sample chunk text" not in user_msg
        assert "Reranked chunk" in user_msg

    async def test_preserves_existing_state_keys(self) -> None:
        llm = MockLLMService(response="Answer.")
        settings = Settings()
        node = GeneratorNode(llm=llm, settings=settings)
        state = make_post_rerank_state(num_chunks=1)

        result = await node.execute(state)

        assert result["query"] == state["query"]
        assert result["rewritten_query"] == state["rewritten_query"]
        assert result["classification"] == state["classification"]
        assert "generated_answer" in result
