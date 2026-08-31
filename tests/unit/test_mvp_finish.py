"""M10 / M11 / M12 — the three remaining MVP modules, service-level tests.

M10 cache write-back: payload shape (D5 admin_only, D37 document_ids, §9 base),
faithful-only discipline is the route's, fail-soft on embedder/store errors.
M11 audit: append-only row shape, escalation pairing, fail-soft.
M12 rate limit: fixed-window allow/deny, first-hit EXPIRE, window isolation.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services.audit import record_escalation, record_query_outcome
from app.services.cache_writer import write_cache_entry
from app.services.rate_limit import is_allowed
from tests.conftest import MockRedis


def _chunks():
    return [
        {"chunk_id": "c1", "document_id": "d1"},
        {"chunk_id": "c2", "document_id": "d1"},
        {"chunk_id": "c3", "document_id": "d2"},
    ]


class TestM10CacheWriteBack:
    async def test_payload_carries_d5_admin_only_and_d37_document_ids(self) -> None:
        store = MagicMock()
        store.upsert = AsyncMock()
        embedder = MagicMock()
        embedder.embed_single = AsyncMock(return_value=[0.1] * 8)
        settings = SimpleNamespace(
            QDRANT_CACHE_COLLECTION="semantic_cache", CACHE_TTL_HOURS=24
        )

        await write_cache_entry(
            query="q",
            answer="a",
            chunks=_chunks(),
            citations=[{"index": 1, "chunk_id": "c1", "image_refs": [{"image_id": "i1"}]}],
            user_role="developer",
            user_is_admin=True,
            embedder=embedder,
            vector_store=store,
            settings=settings,
        )

        store.upsert.assert_awaited_once()
        call = store.upsert.await_args.kwargs
        assert call["collection"] == "semantic_cache"
        payload = call["points"][0]["payload"]
        # §9 base fields
        assert payload["query_text"] == "q"
        assert payload["answer_text"] == "a"
        assert payload["role"] == "developer"
        assert payload["chunk_ids"] == ["c1", "c2", "c3"]
        assert payload["ttl_hours"] == 24
        assert "created_at" in payload
        # D5 — the privilege dimension, mirrored at write time
        assert payload["admin_only"] is True
        # D37 — document granularity, so dropped-region re-ingests invalidate
        assert payload["document_ids"] == ["d1", "d2"]
        # The polish travels with the answer: a hit must render hover cards and
        # images exactly like the full pipeline, not raw [N] markers.
        assert payload["citations"][0]["image_refs"] == [{"image_id": "i1"}]

    async def test_non_admin_entry_is_not_privileged(self) -> None:
        store = MagicMock()
        store.upsert = AsyncMock()
        embedder = MagicMock()
        embedder.embed_single = AsyncMock(return_value=[0.1] * 8)

        await write_cache_entry(
            query="q", answer="a", chunks=[], citations=[], user_role="developer",
            user_is_admin=False, embedder=embedder, vector_store=store,
            settings=SimpleNamespace(QDRANT_CACHE_COLLECTION="c", CACHE_TTL_HOURS=24),
        )
        assert store.upsert.await_args.kwargs["points"][0]["payload"]["admin_only"] is False

    async def test_failure_is_soft(self) -> None:
        embedder = MagicMock()
        embedder.embed_single = AsyncMock(side_effect=RuntimeError("ollama down"))
        # Must not raise — the answer is already served by the time this runs.
        await write_cache_entry(
            query="q", answer="a", chunks=[], citations=[], user_role="developer",
            user_is_admin=False, embedder=embedder,
            vector_store=MagicMock(),
            settings=SimpleNamespace(QDRANT_CACHE_COLLECTION="c", CACHE_TTL_HOURS=24),
        )


class TestM11Audit:
    async def test_row_shape_is_append_only_complete(self) -> None:
        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()

        # capture the row the helper constructs
        captured: dict = {}
        import app.services.audit as audit_mod

        real_audit_log = audit_mod.AuditLog

        class _Capturing(real_audit_log):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._kwargs = kwargs
                captured.update(kwargs)
                self.id = uuid.uuid4()

        audit_mod.AuditLog = _Capturing
        try:
            row = await record_query_outcome(
                session,
                user_id=str(uuid.uuid4()),
                query_text="q",
                answer_text="a",
                confidence_score=0.87,
                was_escalated=False,
                cache_hit=False,
                chunks_used=["c1"],
                namespace_queried="documents",
                response_time_ms=1234,
            )
        finally:
            audit_mod.AuditLog = real_audit_log

        assert row is not None
        assert captured["confidence_score"] == 0.87
        assert captured["chunks_used"] == ["c1"]
        assert captured["response_time_ms"] == 1234
        assert captured["was_escalated"] is False
        session.add.assert_called_once()
        session.commit.assert_awaited_once()

    async def test_escalation_pairs_with_the_audit_row(self) -> None:
        session = MagicMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        audit_id = uuid.uuid4()

        event = await record_escalation(session, audit_log_id=audit_id, reason="faithfulness_failure")

        assert event is not None
        assert event.audit_log_id == audit_id
        assert event.reason == "faithfulness_failure"
        assert event.resolved_at is None  # fresh escalation, unresolved

    async def test_audit_failure_is_soft(self) -> None:
        session = MagicMock()
        session.flush = AsyncMock(side_effect=RuntimeError("db gone"))
        session.rollback = AsyncMock()

        row = await record_query_outcome(
            session, user_id=uuid.uuid4(), query_text="q", answer_text="a"
        )
        assert row is None
        session.rollback.assert_awaited_once()


class TestM12RateLimit:
    async def test_allows_under_the_budget(self) -> None:
        redis = MockRedis()
        for _ in range(10):
            assert await is_allowed(redis, user_id="u", max_requests=10, window_seconds=60)

    async def test_denies_over_the_budget(self) -> None:
        redis = MockRedis()
        for _ in range(10):
            await is_allowed(redis, user_id="u", max_requests=10, window_seconds=60)
        assert not await is_allowed(redis, user_id="u", max_requests=10, window_seconds=60)

    async def test_users_are_isolated(self) -> None:
        redis = MockRedis()
        for _ in range(10):
            await is_allowed(redis, user_id="u1", max_requests=10, window_seconds=60)
        assert await is_allowed(redis, user_id="u2", max_requests=10, window_seconds=60)

    async def test_first_hit_sets_the_window_ttl(self) -> None:
        redis = MockRedis()
        await is_allowed(redis, user_id="u", max_requests=10, window_seconds=60)
        # The counter key exists and holds "1"; the mock's expire returns True
        # only for existing keys, which is the observable half of the contract.
        assert redis._store[f"ratelimit:u:{__import__('time').time() // 60:.0f}"] == "1"


class TestCachedAnswerKeepsItsPolish:
    """Node 0 rebuilds citations from the entry payload — the found-in-manual-
    testing defect was a text-only cache hit serving raw [N] markers with no
    images. Route-level rendering is covered by the markup tests; this pins
    the node's state contract."""

    async def test_hit_returns_citations_from_the_payload(self) -> None:
        from app.pipeline.nodes.node_00_cache_check import CacheCheckNode

        citations = [
            {
                "index": 1,
                "chunk_id": "c1",
                "document_id": "d1",
                "text": "chunk text",
                "section_path": "A > B",
                "anchor": "bkmrk-x",
                "image_refs": [{"image_id": "i1", "anchor": "bkmrk-x"}],
                "original_filename": "doc.html",
            }
        ]
        embedder = MagicMock()
        embedder.embed_single = AsyncMock(return_value=[0.1])
        store = MagicMock()
        store.search = AsyncMock(
            return_value=[
                {"score": 0.97, "text": "cached answer", "citations": citations}
            ]
        )
        node = CacheCheckNode(
            embedder=embedder,
            vector_store=store,
            settings=SimpleNamespace(
                QDRANT_CACHE_COLLECTION="semantic_cache",
                CACHE_SIMILARITY_THRESHOLD=0.92,
            ),
        )

        out = await node.execute(
            {"query": "q", "user_role": "developer", "user_is_admin": False}
        )

        assert out["cache_hit"] is True
        assert out["cached_answer"] == "cached answer"
        assert out["citations"] == citations
