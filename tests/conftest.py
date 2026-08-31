from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy.engine.url import make_url

from app.core.security import BaseAuthService, UserSession
from app.core.services.embedder import BaseEmbedder
from app.core.services.llm import BaseLLMService
from app.core.services.reranker import BaseReranker
from app.core.services.vector_store import BaseVectorStore
from app.core.settings import settings
from app.core.state import ChunkPayload, PipelineState

# ---------------------------------------------------------------------------
# Integration-test database guard
# ---------------------------------------------------------------------------


def require_test_database_url(suite: str) -> str:
    """Return TEST_DATABASE_URL, skipping if unset and failing if it is the dev DB.

    Every integration fixture that runs `alembic downgrade base` must call this
    first. Downgrade drops every table, so aiming it at the development database
    destroys it silently — the suite still passes.

    Compares DATABASE NAMES, not URL strings. The previous guard was
    `if test_url == str(settings.DATABASE_URL)`, which a difference in spelling
    defeats while both URLs still address the same physical database:

        settings.DATABASE_URL = ...@postgres:5432/whitecape    # .env, container name
        TEST_DATABASE_URL     = ...@localhost:5432/whitecape   # host-side tooling

    Those strings are unequal, so the old guard passed — and then wiped the dev
    schema. That configuration was live in this repo until `.env.local` landed
    (see its header), so this is a latent hazard that was actually reachable, not
    a hypothetical one. `127.0.0.1` vs `localhost`, an added `?ssl=` query, and a
    `postgresql://` vs `postgresql+asyncpg://` driver prefix defeat it the same way.

    Comparing names alone is sufficient and needs no knowledge of which hostnames
    happen to resolve to the same server: if the names differ, they are different
    databases on any host; if they match, refuse regardless of how the host is
    spelled. Distinct names are the only thing that actually separates them.
    """
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip(f"TEST_DATABASE_URL not set — {suite} integration tests skipped")

    test_db = make_url(test_url).database
    dev_db = make_url(str(settings.DATABASE_URL)).database

    if not test_db:
        pytest.fail(f"TEST_DATABASE_URL names no database: {test_url!r}")

    if test_db == dev_db:
        pytest.fail(
            f"TEST_DATABASE_URL and settings.DATABASE_URL both name the database "
            f"{test_db!r}. These tests run `alembic downgrade base`, which would "
            f"drop every table in the development database. Point TEST_DATABASE_URL "
            f"at a distinct database name (e.g. {dev_db}_test)."
        )

    return test_url


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
        "user_is_admin": False,
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
        # A NEW list, not a shared one: delete_by_any is a different operation from
        # delete_by_filter, and folding both into delete_calls would silently change
        # what every prior test's `delete_calls == []` assertion means (M6 mock
        # contract, additive only — the rule MockRedis was extended under).
        self.delete_by_any_calls: list[dict] = []

    async def search(
        self,
        collection: str,
        query_vector: list[float],
        allowed_roles: list[str],
        user_is_admin: bool,
        limit: int = 10,
    ) -> list[ChunkPayload]:
        self.search_calls.append(
            {
                "collection": collection,
                "query_vector": query_vector,
                "allowed_roles": allowed_roles,
                "limit": limit,
                "user_is_admin": user_is_admin,
            }
        )
        return self._results[:limit]

    async def upsert(self, collection: str, points: list[dict]) -> None:
        self.upsert_calls.append({"collection": collection, "points": points})

    async def delete_by_filter(self, collection: str, filter_conditions: dict) -> None:
        self.delete_calls.append({"collection": collection, "filter": filter_conditions})

    async def delete_by_any(self, collection: str, key: str, values: list[str]) -> None:
        self.delete_by_any_calls.append(
            {"collection": collection, "key": key, "values": values}
        )


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
        # Set keyspace (M9): user_sessions:{user_id} reverse index. Kept separate
        # from _store so the string store stays a string store — get/setex types
        # are unchanged.
        self._sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._store[key] = value
        self._ttls[key] = ttl

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self._ttls.pop(key, None)
        # Real DEL removes a key whatever its type, and purge_user_sessions
        # deletes the reverse-index set by key. No pre-M9 test puts anything in
        # _sets, so behaviour for every existing caller is unchanged.
        self._sets.pop(key, None)

    async def aclose(self) -> None:
        pass

    # --- Set commands (M9, additive) ---

    async def sadd(self, key: str, *values: str) -> int:
        members = self._sets.setdefault(key, set())
        before = len(members)
        members.update(values)
        return len(members) - before

    async def smembers(self, key: str) -> set[str]:
        # A copy: callers iterate while deleting keys (purge_user_sessions).
        return set(self._sets.get(key, set()))

    async def srem(self, key: str, *values: str) -> int:
        members = self._sets.get(key)
        if not members:
            return 0
        removed = len(members & set(values))
        members.difference_update(values)
        return removed

    async def expire(self, key: str, ttl: int) -> bool:
        # Records the TTL without enforcing it — nothing in the suite advances
        # time, and an enforcing mock would make tests depend on wall clock.
        self._ttls[key] = ttl
        return True


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
        # True — the common case for every test that predates M9b: a user whose
        # role has been decided. A first-login user (role_source="default") is the
        # exception and passes role_confirmed=False explicitly.
        "role_confirmed": True,
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
        # Explicit rather than left to the column's server_default: an unflushed
        # or freshly flushed model reads None for a server-default column, and
        # `role_source != "default"` would then report an unconfirmed user as
        # confirmed. Tests that want the confirmed case pass "admin_assigned".
        "role_source": "default",
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


# ---------------------------------------------------------------------------
# Frontend / search test helpers
# ---------------------------------------------------------------------------


def make_faithful_pipeline_state(**overrides) -> PipelineState:
    """Post-pipeline state simulating a successful, faithful answer."""
    state = make_post_generation_state()
    state["is_faithful"] = True
    state["faithfulness_score"] = 0.85
    state.update(overrides)
    return state


def make_unfaithful_pipeline_state(**overrides) -> PipelineState:
    """Post-pipeline state simulating a faithfulness check failure."""
    state = make_post_generation_state()
    state["is_faithful"] = False
    state["faithfulness_score"] = 0.2
    state.update(overrides)
    return state


def make_direct_pipeline_state(**overrides) -> PipelineState:
    """Post-pipeline state simulating a DIRECT classification (no retrieval)."""
    state = make_state()
    state["classification"] = "DIRECT"
    state["cache_hit"] = False
    state["direct_response"] = "I can only answer from Whitecape's indexed documents."
    state.update(overrides)
    return state


def make_cache_hit_pipeline_state(**overrides) -> PipelineState:
    """Post-pipeline state simulating a semantic cache hit."""
    state = make_state()
    state["cache_hit"] = True
    state["cached_answer"] = "Cached: employees get 25 days of annual leave."
    state.update(overrides)
    return state


def make_authenticated_redis(
    user: UserSession | None = None,
    session_id: str = "test-session-id",
) -> tuple[MockRedis, str]:
    """Create a MockRedis pre-loaded with a valid user session.

    Returns (mock_redis, session_id) — set the session_id as the cookie value.
    """
    if user is None:
        user = make_user_session()
    redis = MockRedis(data={f"session:{session_id}": serialize_user_session(user)})
    return redis, session_id


# ---------------------------------------------------------------------------
# Image / document seed helpers (image_serving_route integration tests)
# ---------------------------------------------------------------------------

# The developer role seeded by the initial migration
# (alembic/versions/ddb267266304_initial_schema.py). Documents need an uploader,
# and the uploader needs a real role_id — this is the one guaranteed to exist.
SEED_ROLE_DEVELOPER_ID = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000001")


def make_image_bytes(size: int = 1024) -> bytes:
    """Realistic random image payload — os.urandom so each blob hashes uniquely."""
    return os.urandom(size)


async def seed_document_with_image(
    session,
    document_id,
    image_id,
    image_bytes,
    restricted,
    tmp_upload_dir,
):
    """Insert one Document + its junction row and write the image file to disk.

    Self-contained: also inserts an uploader User (Document.uploaded_by is NOT NULL),
    keyed uniquely off document_id so repeated calls never collide. The file lands
    under `tmp_upload_dir/derived/{image_id}.png` — the only directory the route
    serves from. Returns (document_id, image_id).
    """
    from app.core.models.document import Document
    from app.core.models.document_image import DocumentImage
    from app.core.models.user import User

    uploader = User(
        id=uuid.uuid4(),
        email=f"uploader-{document_id}@seed.test",
        keycloak_id=f"kc-{document_id}",
        role_id=SEED_ROLE_DEVELOPER_ID,
        is_admin=True,
        is_owner=False,
    )
    session.add(uploader)
    await session.flush()

    document = Document(
        id=document_id,
        source_path=f"/seed/{document_id}.pdf",
        original_filename=f"{document_id}.pdf",
        category="technical",
        restricted=restricted,
        doc_hash=hashlib.sha256(f"doc-{document_id}".encode()).hexdigest(),
        uploaded_by=uploader.id,
        chunk_count=0,
        ingestion_status="done",
    )
    session.add(document)
    await session.flush()

    session.add(DocumentImage(document_id=document.id, image_id=image_id))
    await session.flush()

    derived = Path(tmp_upload_dir) / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    (derived / f"{image_id}.png").write_bytes(image_bytes)

    await session.commit()
    return (document.id, image_id)


# ---------------------------------------------------------------------------
# Qdrant bootstrap mock (M2)
# ---------------------------------------------------------------------------


class _FakeCollectionInfo:
    """Return value of get_collection — only .payload_schema is read by bootstrap."""

    def __init__(self, payload_schema: dict | None):
        self.payload_schema = payload_schema


class FakeQdrantAdmin:
    """Recording double for AsyncQdrantClient's admin surface (contract M2 line 91).

    Records the full kwargs of every create_collection / create_payload_index call
    because the assertions are about the *values* passed (size, distance, schema),
    not merely that a call happened. Existing collections and their already-indexed
    fields are test-settable. Simulates no Qdrant errors unless a test sets them.
    """

    def __init__(self) -> None:
        # Test-settable state:
        self.existing_collections: set[str] = set()
        self.payload_schemas: dict[str, dict] = {}  # collection -> {field: schema}
        # Recorded calls:
        self.create_collection_calls: list[dict] = []
        self.create_payload_index_calls: list[dict] = []
        self.collection_exists_calls: list[str] = []
        self.get_collection_calls: list[str] = []
        self.delete_collection_calls: list[str] = []
        self.delete_calls: list[dict] = []

    async def collection_exists(self, collection_name: str) -> bool:
        self.collection_exists_calls.append(collection_name)
        return collection_name in self.existing_collections

    async def get_collection(self, collection_name: str) -> _FakeCollectionInfo:
        self.get_collection_calls.append(collection_name)
        return _FakeCollectionInfo(self.payload_schemas.get(collection_name, {}))

    async def create_collection(self, collection_name: str, vectors_config, **kwargs) -> None:
        self.create_collection_calls.append(
            {"collection_name": collection_name, "vectors_config": vectors_config, **kwargs}
        )

    async def create_payload_index(
        self, collection_name: str, field_name: str, field_schema, **kwargs
    ) -> None:
        self.create_payload_index_calls.append(
            {
                "collection_name": collection_name,
                "field_name": field_name,
                "field_schema": field_schema,
                **kwargs,
            }
        )

    async def delete_collection(self, collection_name: str, **kwargs) -> None:
        self.delete_collection_calls.append(collection_name)

    async def delete(self, collection_name: str, **kwargs) -> None:
        self.delete_calls.append({"collection_name": collection_name, **kwargs})

    async def close(self) -> None:  # bootstrap only closes clients it owns; never here
        pass


@pytest.fixture
def fake_qdrant_admin() -> FakeQdrantAdmin:
    return FakeQdrantAdmin()
