from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.core.services.embedder import BaseEmbedder
from app.core.services.llm import BaseLLMService
from app.core.services.reranker import BaseReranker
from app.core.services.vector_store import BaseVectorStore
from app.core.state import ChunkPayload, PipelineState


# ---------------------------------------------------------------------------
# State factories
# ---------------------------------------------------------------------------


def make_chunk(**overrides) -> ChunkPayload:
    defaults: ChunkPayload = {
        "chunk_id": str(uuid.uuid4()),
        "text": "Sample chunk text for testing.",
        "document_id": str(uuid.uuid4()),
        "original_filename": "test_doc.pdf",
        "category": "internal",
        "page_number": 1,
        "chunk_index": 0,
        "score": 0.85,
    }
    defaults.update(overrides)
    return defaults


def make_state(**overrides) -> PipelineState:
    defaults: PipelineState = {
        "query": "What is the company leave policy?",
        "user_id": str(uuid.uuid4()),
        "user_role": "employee",
        "user_email": "test@whitecape.fr",
    }
    defaults.update(overrides)
    return defaults


def make_post_classifier_state(
    classification: str = "SIMPLE_RAG", **overrides
) -> PipelineState:
    state = make_state()
    state["classification"] = classification
    if classification == "DIRECT":
        state["direct_response"] = "I can only answer from Whitecape's indexed documents."
    state.update(overrides)
    return state


def make_post_retrieval_state(num_chunks: int = 5, **overrides) -> PipelineState:
    state = make_post_classifier_state()
    state["rewritten_query"] = "company leave policy details"
    state["sub_queries"] = []
    state["retrieved_chunks"] = [
        make_chunk(text=f"Chunk {i} about leave policy.", chunk_index=i, score=0.9 - i * 0.05)
        for i in range(num_chunks)
    ]
    state.update(overrides)
    return state


def make_post_rerank_state(num_chunks: int = 3, **overrides) -> PipelineState:
    state = make_post_retrieval_state(num_chunks=5)
    state["reranked_chunks"] = [
        make_chunk(text=f"Reranked chunk {i}.", chunk_index=i, score=0.95 - i * 0.1)
        for i in range(num_chunks)
    ]
    state["relevance_pass"] = True
    state.update(overrides)
    return state


def make_post_generation_state(**overrides) -> PipelineState:
    state = make_post_rerank_state()
    state["generated_answer"] = (
        "According to the company handbook, employees are entitled to 25 days "
        "of annual leave per year [source: leave_policy.pdf, p.3]."
    )
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Mock services
# ---------------------------------------------------------------------------


class MockLLMService(BaseLLMService):
    def __init__(self, response: str = "Mock LLM response."):
        self._response = response
        self.calls: list[dict] = []

    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
    ) -> str:
        self.calls.append({"model": model, "messages": messages, "temperature": temperature})
        return self._response


class MockVectorStore(BaseVectorStore):
    def __init__(self, results: list[ChunkPayload] | None = None):
        self._results = results or [make_chunk()]
        self.search_calls: list[dict] = []
        self.upsert_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        allowed_roles: list[str],
        limit: int = 10,
    ) -> list[ChunkPayload]:
        self.search_calls.append({
            "collection": collection,
            "query_vector": query_vector,
            "allowed_roles": allowed_roles,
            "limit": limit,
        })
        return self._results[:limit]

    async def upsert(self, collection: str, points: list[dict]) -> None:
        self.upsert_calls.append({"collection": collection, "points": points})

    async def delete_by_filter(self, collection: str, filter_conditions: dict) -> None:
        self.delete_calls.append({"collection": collection, "filter": filter_conditions})


class MockReranker(BaseReranker):
    def __init__(self, scores: list[float] | None = None):
        self._scores = scores or [0.9, 0.7, 0.3]
        self.calls: list[dict] = []

    async def rerank(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append({"query": query, "passages": passages})
        return self._scores[: len(passages)]


class MockEmbedder(BaseEmbedder):
    def __init__(self, vector_dim: int = 1024):
        self._dim = vector_dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.1] * self._dim for _ in texts]

    async def embed_single(self, text: str) -> list[float]:
        result = await self.embed([text])
        return result[0]


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm():
    return MockLLMService()


@pytest.fixture
def mock_vector_store():
    return MockVectorStore()


@pytest.fixture
def mock_reranker():
    return MockReranker()


@pytest.fixture
def mock_embedder():
    return MockEmbedder()


@pytest.fixture
def sample_state():
    return make_state()


@pytest.fixture
def post_classifier_state():
    return make_post_classifier_state()


@pytest.fixture
def post_retrieval_state():
    return make_post_retrieval_state()


@pytest.fixture
def post_rerank_state():
    return make_post_rerank_state()


@pytest.fixture
def post_generation_state():
    return make_post_generation_state()


@pytest.fixture
def sample_chunks():
    return [
        make_chunk(text=f"Chunk number {i}", chunk_index=i, score=0.9 - i * 0.1)
        for i in range(5)
    ]
