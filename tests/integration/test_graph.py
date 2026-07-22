"""Integration tests for the full LangGraph pipeline.

Tests the wiring — routing between nodes, state preservation, and conditional edges.
Individual node logic is tested in unit tests; this tests that the graph connects them correctly.
"""

from __future__ import annotations

import pytest

from app.core.state import PipelineState
from app.core.settings import Settings
from app.pipeline.graph import build_pipeline
from app.pipeline.nodes.node_00_cache_check import CacheCheckNode
from app.pipeline.nodes.node_01_classifier import ClassifierNode
from app.pipeline.nodes.node_02_rewriter_decomposer import RewriterNode
from app.pipeline.nodes.node_03_parallel_qdrant_search import QdrantSearchNode
from app.pipeline.nodes.node_04_reranker import RerankerNode
from app.pipeline.nodes.node_05_relevance_gate import RelevanceGateNode
from app.pipeline.nodes.node_05b_retry import RetryNode
from app.pipeline.nodes.node_06_generator import GeneratorNode
from app.pipeline.nodes.node_07_faithfulness import FaithfulnessNode
from tests.conftest import MockEmbedder, MockLLMService, MockReranker, MockVectorStore, make_chunk


def _build_nodes(
    llm_classifier: MockLLMService | None = None,
    llm_rewriter: MockLLMService | None = None,
    llm_generator: MockLLMService | None = None,
    llm_retry: MockLLMService | None = None,
    embedder: MockEmbedder | None = None,
    vector_store: MockVectorStore | None = None,
    reranker: MockReranker | None = None,
    settings: Settings | None = None,
) -> dict:
    """Create a dict of all pipeline nodes with mock services."""
    settings = settings or Settings()
    return {
        "cache_check": CacheCheckNode(
            embedder=embedder or MockEmbedder(),
            vector_store=vector_store or MockVectorStore(results=[]),
            settings=settings,
        ),
        "classifier": ClassifierNode(
            llm=llm_classifier or MockLLMService(response="SIMPLE_RAG"),
            settings=settings,
        ),
        "rewriter": RewriterNode(
            llm=llm_rewriter or MockLLMService(response="rewritten query"),
            settings=settings,
        ),
        "qdrant_search": QdrantSearchNode(
            embedder=embedder or MockEmbedder(),
            vector_store=vector_store or MockVectorStore(results=[make_chunk()]),
            settings=settings,
        ),
        "reranker": RerankerNode(
            reranker=reranker or MockReranker(scores=[0.9]),
            settings=settings,
        ),
        "relevance_gate": RelevanceGateNode(settings=settings),
        "retry": RetryNode(
            llm=llm_retry or MockLLMService(response="broadened query"),
            settings=settings,
        ),
        "generator": GeneratorNode(
            llm=llm_generator or MockLLMService(response="Generated answer."),
            settings=settings,
        ),
        "faithfulness": FaithfulnessNode(settings=settings),
    }


def _make_input(query: str = "What is the leave policy?") -> PipelineState:
    return {
        "query": query,
        "user_id": "test-user-id",
        "user_role": "developer",
        "user_email": "test@whitecape.fr",
    }


class TestGraphIntegration:

    async def test_direct_query_short_circuits(self) -> None:
        nodes = _build_nodes(
            llm_classifier=MockLLMService(response="DIRECT"),
        )
        graph = build_pipeline(nodes)

        result = await graph.ainvoke(_make_input(query="hello"))

        assert result["classification"] == "DIRECT"
        assert "direct_response" in result
        assert "rewritten_query" not in result
        assert "retrieved_chunks" not in result
        assert "generated_answer" not in result

    async def test_simple_rag_full_pipeline(self) -> None:
        chunks = [make_chunk(text="Leave policy: 25 days annual leave.", score=0.9)]
        nodes = _build_nodes(
            llm_classifier=MockLLMService(response="SIMPLE_RAG"),
            llm_rewriter=MockLLMService(response="company leave policy details"),
            vector_store=MockVectorStore(results=chunks),
            reranker=MockReranker(scores=[0.95]),
            llm_generator=MockLLMService(response="Employees get 25 days leave [source: handbook.pdf]."),
        )
        graph = build_pipeline(nodes)

        result = await graph.ainvoke(_make_input())

        assert result["classification"] == "SIMPLE_RAG"
        assert result["rewritten_query"] == "company leave policy details"
        assert result["retrieved_chunks"] is not None
        assert result["reranked_chunks"] is not None
        assert result["relevance_pass"] is True
        assert result["generated_answer"] is not None
        assert result["is_faithful"] is True

    async def test_cache_hit_skips_pipeline(self) -> None:
        hit = make_chunk(score=0.95, text="cached answer")
        nodes = _build_nodes(
            vector_store=MockVectorStore(results=[hit]),
        )
        graph = build_pipeline(nodes)

        result = await graph.ainvoke(_make_input(query="cached query"))

        assert result["cache_hit"] is True
        assert result["cached_answer"] == "cached answer"
        assert "classification" not in result
        assert "retrieved_chunks" not in result

    async def test_relevance_fail_then_retry_fail_returns_fallback(self) -> None:
        low_chunk = make_chunk(score=0.2)
        test_settings = Settings(RELEVANCE_THRESHOLD=0.5)
        nodes = _build_nodes(
            llm_classifier=MockLLMService(response="SIMPLE_RAG"),
            reranker=MockReranker(scores=[0.2]),
            settings=test_settings,
        )
        nodes["qdrant_search"] = QdrantSearchNode(
            embedder=MockEmbedder(),
            vector_store=MockVectorStore(results=[low_chunk]),
            settings=test_settings,
        )
        nodes["reranker"] = RerankerNode(
            reranker=MockReranker(scores=[0.2]),
            settings=test_settings,
        )
        graph = build_pipeline(nodes)

        result = await graph.ainvoke(_make_input())

        assert result["relevance_pass"] is False
        assert result["retry_attempted"] is True

    async def test_faithfulness_failure_sets_error(self) -> None:
        chunks = [make_chunk(text="The office is in Paris.", score=0.9)]
        nodes = _build_nodes(
            llm_classifier=MockLLMService(response="SIMPLE_RAG"),
            vector_store=MockVectorStore(results=chunks),
            reranker=MockReranker(scores=[0.95]),
            llm_generator=MockLLMService(response="The moon landing was faked by NASA."),
        )
        graph = build_pipeline(nodes)

        result = await graph.ainvoke(_make_input())

        assert result["is_faithful"] is False
        assert result["faithfulness_score"] < 0.5

    async def test_state_keys_preserved_through_pipeline(self) -> None:
        nodes = _build_nodes(
            llm_classifier=MockLLMService(response="SIMPLE_RAG"),
        )
        graph = build_pipeline(nodes)
        input_state = _make_input(query="leave policy")

        result = await graph.ainvoke(input_state)

        assert result["query"] == "leave policy"
        assert result["user_id"] == "test-user-id"
        assert result["user_role"] == "developer"
        assert result["user_email"] == "test@whitecape.fr"
