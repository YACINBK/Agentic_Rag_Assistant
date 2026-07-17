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
def sample_chunks():
    return [
        make_chunk(text=f"Chunk number {i}", chunk_index=i, score=0.9 - i * 0.1)
        for i in range(5)
    ]
