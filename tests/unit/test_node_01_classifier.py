from __future__ import annotations

import pytest

from app.core.exceptions import ClassificationError
from app.core.settings import Settings
from app.pipeline.nodes.node_01_classifier import ClassifierNode
from tests.conftest import MockLLMService, make_state


class TestClassifierNode:

    async def test_chitchat_classified_as_direct(self) -> None:
        mock_llm = MockLLMService(response="DIRECT")
        settings = Settings()
        node = ClassifierNode(llm=mock_llm, settings=settings)
        state = make_state(query="hello!")

        result = await node.execute(state)

        assert result["classification"] == "DIRECT"
        assert isinstance(result["direct_response"], str)
        assert len(result["direct_response"]) > 0
        assert len(mock_llm.calls) == 1

    async def test_domain_question_classified_as_simple_rag(self) -> None:
        mock_llm = MockLLMService(response="SIMPLE_RAG")
        settings = Settings()
        node = ClassifierNode(llm=mock_llm, settings=settings)
        state = make_state(query="What is our deployment process?")

        result = await node.execute(state)

        assert result["classification"] == "SIMPLE_RAG"
        assert "direct_response" not in result
        assert len(mock_llm.calls) == 1

    async def test_manual_mode_skips_llm(self) -> None:
        mock_llm = MockLLMService(response="DIRECT")
        settings = Settings(CLASSIFIER_MODEL="manual")
        node = ClassifierNode(llm=mock_llm, settings=settings)
        state = make_state(query="anything")

        result = await node.execute(state)

        assert result["classification"] == "SIMPLE_RAG"
        assert len(mock_llm.calls) == 0

    async def test_invalid_llm_response_defaults_to_simple_rag(self) -> None:
        mock_llm = MockLLMService(response="banana soup invalid garbage")
        settings = Settings()
        node = ClassifierNode(llm=mock_llm, settings=settings)
        state = make_state(query="What is the leave policy?")

        result = await node.execute(state)

        assert result["classification"] == "SIMPLE_RAG"

    async def test_out_of_scope_classified_as_direct(self) -> None:
        mock_llm = MockLLMService(response="DIRECT")
        settings = Settings()
        node = ClassifierNode(llm=mock_llm, settings=settings)
        state = make_state(query="What's the weather today?")

        result = await node.execute(state)

        assert result["classification"] == "DIRECT"
        assert isinstance(result["direct_response"], str)
        assert len(result["direct_response"]) > 0

    async def test_missing_query_raises_classification_error(self) -> None:
        mock_llm = MockLLMService(response="SIMPLE_RAG")
        settings = Settings()
        node = ClassifierNode(llm=mock_llm, settings=settings)
        state = make_state()
        del state["query"]

        with pytest.raises(ClassificationError):
            await node.execute(state)

    async def test_empty_query_raises_classification_error(self) -> None:
        mock_llm = MockLLMService(response="SIMPLE_RAG")
        settings = Settings()
        node = ClassifierNode(llm=mock_llm, settings=settings)
        state = make_state(query="   ")

        with pytest.raises(ClassificationError):
            await node.execute(state)

    async def test_llm_called_with_correct_model(self) -> None:
        mock_llm = MockLLMService(response="SIMPLE_RAG")
        settings = Settings(CLASSIFIER_MODEL="ollama/qwen2.5:7b")
        node = ClassifierNode(llm=mock_llm, settings=settings)
        state = make_state(query="What is the leave policy?")

        await node.execute(state)

        assert len(mock_llm.calls) == 1
        assert mock_llm.calls[0]["model"] == "ollama/qwen2.5:7b"

    async def test_preserves_existing_state_keys(self) -> None:
        mock_llm = MockLLMService(response="SIMPLE_RAG")
        settings = Settings()
        node = ClassifierNode(llm=mock_llm, settings=settings)
        state = make_state(query="test question", user_role="developer")

        result = await node.execute(state)

        assert result["query"] == "test question"
        assert result["user_role"] == "developer"
        assert result["classification"] == "SIMPLE_RAG"
