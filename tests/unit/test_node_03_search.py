from __future__ import annotations

import pytest

from app.core.exceptions import RetrievalError
from app.core.settings import Settings
from app.pipeline.nodes.node_03_parallel_qdrant_search import QdrantSearchNode
from tests.conftest import MockEmbedder, MockVectorStore, make_chunk, make_post_classifier_state


class TestQdrantSearchNode:

    async def test_embeds_and_searches_with_role_filter(self) -> None:
        chunks = [make_chunk(), make_chunk()]
        embedder = MockEmbedder()
        store = MockVectorStore(results=chunks)
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(
            rewritten_query="deployment process steps",
            user_role="developer",
        )

        result = await node.execute(state)

        assert len(result["retrieved_chunks"]) == 2
        assert len(embedder.calls) == 1
        assert len(store.search_calls) == 1
        assert store.search_calls[0]["allowed_roles"] == ["developer", "all"]

    async def test_empty_results_returns_empty_list(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore(results=[])
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(rewritten_query="nonexistent topic")

        result = await node.execute(state)

        assert result["retrieved_chunks"] == []

    async def test_missing_rewritten_query_raises_error(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore()
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state()
        state.pop("rewritten_query", None)

        with pytest.raises(RetrievalError):
            await node.execute(state)

    async def test_preserves_existing_state_keys(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore(results=[make_chunk()])
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(
            rewritten_query="company policy",
            user_role="employee",
        )

        result = await node.execute(state)

        assert result["query"] == state["query"]
        assert result["rewritten_query"] == "company policy"
        assert result["classification"] == "SIMPLE_RAG"
        assert result["user_role"] == "employee"
        assert "retrieved_chunks" in result

    async def test_role_filter_includes_all(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore(results=[make_chunk()])
        settings = Settings()
        node = QdrantSearchNode(embedder=embedder, vector_store=store, settings=settings)
        state = make_post_classifier_state(
            rewritten_query="test query",
            user_role="qa_engineer",
        )

        await node.execute(state)

        assert store.search_calls[0]["allowed_roles"] == ["qa_engineer", "all"]
