"""M0: two-dimension privilege filter (allowed_roles + admin_only).

Two-layer structure per contract m0_privilege_dimension:
- Filter shape (tests 1–3): QdrantVectorStore + local recording AsyncQdrantClient double.
- Passthrough (tests 7–8): pipeline nodes + existing MockVectorStore from conftest.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.error_handlers import register_error_handlers
from app.api.routes import search_router
from app.core.models.role import Role
from app.core.models.user import User
from app.core.settings import Settings
from app.core.state import PipelineState
from app.pipeline.nodes.node_00_cache_check import CacheCheckNode
from app.pipeline.nodes.node_03_parallel_qdrant_search import QdrantSearchNode
from app.services.vector_store import QdrantVectorStore
from tests.conftest import (
    MockEmbedder,
    MockRedis,
    MockVectorStore,
    make_chunk,
    make_state,
    make_user_session,
    serialize_user_session,
)

SESSION_ID = "abc123"


# ---------------------------------------------------------------------------
# Recording AsyncQdrantClient double (local to this file — contract Environment)
# ---------------------------------------------------------------------------


class _RecordingQdrantClient:
    """Captures query_points kwargs verbatim; returns one well-formed point."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def query_points(self, **kwargs):
        self.calls.append(kwargs)
        point = SimpleNamespace(
            id="point-1",
            score=0.9,
            payload={
                "text": "chunk text",
                "document_id": "doc-1",
                "original_filename": "doc.pdf",
                "category": "technical",
                "page_number": 1,
                "chunk_index": 0,
            },
        )
        return SimpleNamespace(points=[point])


def _make_stream_app(redis: MockRedis) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    register_error_handlers(app)
    app.include_router(search_router)
    return app


# ---------------------------------------------------------------------------
# 1–3: filter shape (QdrantVectorStore + recording double)
# ---------------------------------------------------------------------------


class TestFilterShape:

    async def test_non_admin_filter_has_both_dimensions(self) -> None:
        client = _RecordingQdrantClient()
        store = QdrantVectorStore(client=client)

        await store.search(
            collection="documents",
            query_vector=[0.0] * 1024,
            allowed_roles=["developer", "all"],
            user_is_admin=False,
        )

        query_filter = client.calls[0]["query_filter"]
        assert len(query_filter.must) == 2

        role_cond = next(c for c in query_filter.must if c.key == "allowed_roles")
        assert role_cond.match.any == ["developer", "all"]

        priv_cond = next(c for c in query_filter.must if c.key == "admin_only")
        assert priv_cond.match.value is False

        assert "admin" not in role_cond.match.any
        assert "owner" not in role_cond.match.any

    async def test_admin_filter_omits_privilege_condition(self) -> None:
        client = _RecordingQdrantClient()
        store = QdrantVectorStore(client=client)

        await store.search(
            collection="documents",
            query_vector=[0.0] * 1024,
            allowed_roles=["developer", "all"],
            user_is_admin=True,
        )

        query_filter = client.calls[0]["query_filter"]
        assert len(query_filter.must) == 1
        assert all(c.key != "admin_only" for c in query_filter.must)

    async def test_search_requires_privilege_argument(self) -> None:
        store = QdrantVectorStore(client=_RecordingQdrantClient())

        with pytest.raises(TypeError):
            await store.search(
                collection="documents",
                query_vector=[0.0] * 1024,
                allowed_roles=["developer"],
            )


# ---------------------------------------------------------------------------
# 4: stream route populates user_is_admin in pipeline state
# ---------------------------------------------------------------------------


class TestStreamPopulatesFlag:

    @pytest.mark.parametrize("is_admin", [True, False])
    async def test_stream_populates_user_is_admin(self, is_admin: bool) -> None:
        session = make_user_session(is_admin=is_admin)
        redis = MockRedis()
        redis._store[f"session:{SESSION_ID}"] = serialize_user_session(session)
        redis._store["query:qid-1"] = "test query"
        client = TestClient(_make_stream_app(redis))
        client.cookies.set("session_id", SESSION_ID)

        captured: dict = {}
        graph = MagicMock()

        async def fake_ainvoke(state):
            captured.update(state)
            return {"is_faithful": True, "generated_answer": "answer"}

        graph.ainvoke = fake_ainvoke
        with patch(
            "app.api.routes.search.get_compiled_pipeline", return_value=graph
        ):
            client.get("/search/stream?qid=qid-1")

        assert captured["user_is_admin"] is is_admin


# ---------------------------------------------------------------------------
# 5: Role.name rejects reserved words
# ---------------------------------------------------------------------------


class TestRoleNameValidator:

    @pytest.mark.parametrize(
        "value", ["all", "All", "ALL", "admin", "Admin", "owner", "OWNER"]
    )
    def test_role_name_rejects_reserved_words(self, value: str) -> None:
        with pytest.raises(ValueError):
            Role(name=value)

    def test_role_name_accepts_valid_name(self) -> None:
        Role(name="developer")


# ---------------------------------------------------------------------------
# 6: static invariants (assertions 1, 9, 10)
# ---------------------------------------------------------------------------


class TestStaticInvariants:

    def test_static_invariants(self) -> None:
        # Assertion 1: PipelineState declares user_is_admin
        assert "user_is_admin" in PipelineState.__annotations__

        # Assertion 9: ck_owner_implies_admin + idx_single_owner in User.__table_args__
        arg_names = {getattr(a, "name", None) for a in User.__table_args__}
        assert "ck_owner_implies_admin" in arg_names
        assert "idx_single_owner" in arg_names

        # Assertion 10: no `is_admin or is_owner` expression under app/
        app_dir = Path(__file__).resolve().parents[2] / "app"
        for py_file in app_dir.rglob("*.py"):
            if py_file.name in ("dev.py", "dev_fake.py"):
                continue
            assert "is_admin or is_owner" not in py_file.read_text(), py_file


# ---------------------------------------------------------------------------
# 7–8: node passthrough (existing MockVectorStore from conftest)
# ---------------------------------------------------------------------------


class TestNodePassthrough:

    async def test_node03_forwards_privilege_flag(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore(results=[make_chunk()])
        node = QdrantSearchNode(
            embedder=embedder, vector_store=store, settings=Settings()
        )
        state = make_state(
            user_role="developer",
            rewritten_query="deployment steps",
            user_is_admin=False,
        )

        await node.execute(state)

        assert store.search_calls[0]["user_is_admin"] is False

    async def test_cache_lookup_passes_privilege_flag(self) -> None:
        embedder = MockEmbedder()
        store = MockVectorStore(results=[make_chunk(score=0.95)])
        settings = Settings()
        node = CacheCheckNode(
            embedder=embedder, vector_store=store, settings=settings
        )
        state = make_state(user_is_admin=False)

        await node.execute(state)

        assert store.search_calls[0]["user_is_admin"] is False
        assert store.search_calls[0]["collection"] == settings.QDRANT_CACHE_COLLECTION
