from __future__ import annotations

import json
import uuid

import pytest

from app.core.security import BaseAuthService, UserSession
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


def make_post_classifier_state(classification: str = "SIMPLE_RAG", **overrides) -> PipelineState:
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
        self._results = results if results is not None else [make_chunk()]
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
        self.search_calls.append(
            {
                "collection": collection,
                "query_vector": query_vector,
                "allowed_roles": allowed_roles,
                "limit": limit,
            }
        )
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
        make_chunk(text=f"Chunk number {i}", chunk_index=i, score=0.9 - i * 0.1) for i in range(5)
    ]


# ---------------------------------------------------------------------------
# Auth mock services
# ---------------------------------------------------------------------------


class MockAuthService(BaseAuthService):
    """Mock auth service for testing routes without Keycloak."""

    def __init__(self, user: UserSession | None = None):
        self._user = user
        self.calls: list[str] = []

    async def get_authorization_url(self, request) -> str:
        self.calls.append("get_authorization_url")
        return "http://keycloak:8080/realms/whitecape/protocol/openid-connect/auth?client_id=test"

    async def handle_callback(self, request) -> UserSession:
        self.calls.append("handle_callback")
        if self._user is None:
            from app.core.exceptions import AuthenticationError

            raise AuthenticationError("No user configured in mock")
        return self._user

    async def get_current_user(self, request) -> UserSession | None:
        self.calls.append("get_current_user")
        return self._user

    async def logout(self, request) -> str:
        self.calls.append("logout")
        return "http://keycloak:8080/realms/whitecape/protocol/openid-connect/logout?client_id=test"


class MockRedis:
    """In-memory Redis mock for session storage tests."""

    def __init__(self, data: dict[str, str] | None = None):
        self._store: dict[str, str] = data or {}
        self._ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._store[key] = value
        self._ttls[key] = ttl

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self._ttls.pop(key, None)

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Auth state factories
# ---------------------------------------------------------------------------


def make_user_session(**overrides) -> UserSession:
    defaults = {
        "user_id": str(uuid.uuid4()),
        "keycloak_id": str(uuid.uuid4()),
        "email": "test@whitecape.fr",
        "role": "developer",
        "is_admin": False,
        "is_owner": False,
    }
    defaults.update(overrides)
    return UserSession(**defaults)


def make_user_model(**overrides):
    from app.core.models.user import User

    defaults = {
        "id": uuid.uuid4(),
        "email": "test@whitecape.fr",
        "keycloak_id": str(uuid.uuid4()),
        "role_id": uuid.uuid4(),
        "is_admin": False,
        "is_owner": False,
    }
    defaults.update(overrides)
    return User(**defaults)


def make_role_model(**overrides):
    from app.core.models.role import Role

    defaults = {
        "id": uuid.uuid4(),
        "name": "developer",
        "description": None,
        "persona_prompt": None,
    }
    defaults.update(overrides)
    return Role(**defaults)


def serialize_user_session(session: UserSession) -> str:
    """Serialize a UserSession to JSON (same format as KeycloakAuthService stores in Redis)."""
    return json.dumps(session.__dict__)


# ---------------------------------------------------------------------------
# Auth fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_auth_service():
    return MockAuthService(user=make_user_session())


@pytest.fixture
def mock_redis():
    return MockRedis()


@pytest.fixture
def user_session():
    return make_user_session()


@pytest.fixture
def admin_session():
    return make_user_session(is_admin=True)


@pytest.fixture
def owner_session():
    return make_user_session(is_admin=True, is_owner=True)
