"""M8 admin documents UI — integration tests against real PostgreSQL.

Assertions 1, 2, 3, 4, 6 are PostgreSQL/route-branching facts: the full-page vs
partial branch, admin/non-admin/unauthenticated status codes, and — the one a
mock cannot prove — the `ORDER BY ... NULLS FIRST` ordering as PostgreSQL
actually executes it. Template-only facts (assertions 5, 7, 8, 9, 10) live in
tests/unit/test_m8_admin_documents_ui.py instead.

Fixture follows tests/integration/test_admin_upload_route.py's upload_env
structurally, minus the CSRF-wrapped second app: every request here is a GET,
and GET is in starlette_csrf's safe_methods, so no CSRFMiddleware is needed.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.dependencies import SESSION_COOKIE
from app.api.error_handlers import register_error_handlers
from app.api.routes import admin_router
from app.core.models.base import get_session
from app.core.models.document import Document
from app.core.models.user import User
from app.core.settings import settings
from tests.conftest import (
    SEED_ROLE_DEVELOPER_ID,
    MockRedis,
    make_user_session,
    require_test_database_url,
    serialize_user_session,
)

pytestmark = pytest.mark.integration

ADMIN_SESSION_ID = "admin-documents-session"
NONADMIN_SESSION_ID = "nonadmin-documents-session"


@pytest.fixture(scope="module")
def documents_env():
    """Migrate a test DB, seed one uploader, and wire a plain app (no CSRF
    middleware — every request in this suite is a safe GET). NullPool everywhere."""
    test_url = require_test_database_url("admin documents ui")

    admin_user = make_user_session(is_admin=True)
    nonadmin_user = make_user_session(is_admin=False)
    uploader_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "DATABASE_URL", test_url)

        cfg = Config("alembic.ini")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")

        async def _seed() -> None:
            engine = create_async_engine(test_url, poolclass=NullPool)
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            try:
                async with factory() as session:
                    session.add(
                        User(
                            id=uploader_id,
                            email="uploader-documents-ui@seed.test",
                            keycloak_id="kc-documents-ui",
                            role_id=SEED_ROLE_DEVELOPER_ID,
                            is_admin=True,
                            is_owner=False,
                        )
                    )
                    await session.commit()
            finally:
                await engine.dispose()

        asyncio.run(_seed())

        route_engine = create_async_engine(test_url, poolclass=NullPool)
        route_factory = async_sessionmaker(
            route_engine, class_=AsyncSession, expire_on_commit=False
        )

        async def _override_get_session():
            async with route_factory() as session:
                yield session

        redis = MockRedis()
        redis._store[f"session:{ADMIN_SESSION_ID}"] = serialize_user_session(admin_user)
        redis._store[f"session:{NONADMIN_SESSION_ID}"] = serialize_user_session(nonadmin_user)

        app = FastAPI()
        app.state.redis = redis
        register_error_handlers(app)
        app.include_router(admin_router)
        app.dependency_overrides[get_session] = _override_get_session
        client = TestClient(app)

        yield SimpleNamespace(
            client=client,
            test_url=test_url,
            uploader_id=uploader_id,
            admin_session=ADMIN_SESSION_ID,
            nonadmin_session=NONADMIN_SESSION_ID,
        )

        app.dependency_overrides.clear()
        asyncio.run(route_engine.dispose())
        command.downgrade(cfg, "base")


def _get(env, session_id, **kwargs):
    """GET /admin/documents with a clean cookie jar."""
    env.client.cookies.clear()
    if session_id is not None:
        env.client.cookies.set(SESSION_COOKIE, session_id)
    return env.client.get("/admin/documents", **kwargs)


def _insert_document(
    test_url: str,
    uploader_id,
    *,
    original_filename: str,
    ingestion_status: str = "pending",
    last_ingested_at=None,
    category: str = "technical",
    restricted: bool = False,
    chunk_count: int = 0,
) -> None:
    async def _insert() -> None:
        engine = create_async_engine(test_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                session.add(
                    Document(
                        source_path=f"/seed/{original_filename}",
                        original_filename=original_filename,
                        category=category,
                        restricted=restricted,
                        doc_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                        uploaded_by=uploader_id,
                        chunk_count=chunk_count,
                        ingestion_status=ingestion_status,
                        last_ingested_at=last_ingested_at,
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_insert())


def test_full_page_vs_partial(documents_env) -> None:
    """T5 — Assertions 1, 2."""
    _insert_document(
        documents_env.test_url, documents_env.uploader_id, original_filename="doc-a.html"
    )
    _insert_document(
        documents_env.test_url, documents_env.uploader_id, original_filename="doc-b.html"
    )

    full = _get(documents_env, documents_env.admin_session, headers={"accept": "text/html"})
    partial = _get(documents_env, documents_env.admin_session, headers={"HX-Request": "true"})

    assert full.status_code == 200
    assert "<html" in full.text
    assert "Whitecape Knowledge" in full.text
    assert "doc-a.html" in full.text
    assert "doc-b.html" in full.text
    # Both seeded rows default to ingestion_status="pending" (_insert_document),
    # so the route must compute poll=True and both branches must carry the
    # self-refresh trigger (Assertion 7, positive half).
    assert "hx-trigger" in full.text
    assert "hx-trigger" in partial.text

    assert partial.status_code == 200
    assert "<html" not in partial.text
    assert "Whitecape Knowledge" not in partial.text
    assert "doc-a.html" in partial.text
    assert "doc-b.html" in partial.text


def test_ordering_nulls_first_then_filename_then_recency(documents_env) -> None:
    """T6 — Assertion 6. Insert order matches none of the expected output order."""
    new_ts = datetime.now(timezone.utc)
    old_ts = new_ts - timedelta(hours=1)

    # Deliberately scrambled relative to the expected output (a, b, new, old).
    _insert_document(
        documents_env.test_url,
        documents_env.uploader_id,
        original_filename="b.pdf",
        last_ingested_at=None,
    )
    _insert_document(
        documents_env.test_url,
        documents_env.uploader_id,
        original_filename="old.pdf",
        ingestion_status="done",
        last_ingested_at=old_ts,
    )
    _insert_document(
        documents_env.test_url,
        documents_env.uploader_id,
        original_filename="a.pdf",
        last_ingested_at=None,
    )
    _insert_document(
        documents_env.test_url,
        documents_env.uploader_id,
        original_filename="new.pdf",
        ingestion_status="done",
        last_ingested_at=new_ts,
    )

    response = _get(documents_env, documents_env.admin_session, headers={"accept": "text/html"})

    assert response.status_code == 200
    body = response.text
    positions = [body.index(name) for name in ("a.pdf", "b.pdf", "new.pdf", "old.pdf")]
    assert positions == sorted(positions)


def test_authorization_non_admin_and_unauthenticated(documents_env) -> None:
    """T7 — Assertions 3, 4."""
    non_admin_response = _get(
        documents_env, documents_env.nonadmin_session, headers={"accept": "application/json"}
    )
    assert non_admin_response.status_code == 403

    unauthenticated_response = _get(
        documents_env, None, headers={"accept": "application/json"}
    )
    assert unauthenticated_response.status_code == 401


def test_polling_stops_when_nothing_is_in_flight(documents_env) -> None:
    """Retry — Assertion 7, negative half. Defined LAST: it normalizes every
    row in the table to a terminal status, so any test relying on an
    in-flight row must run before this one (T5, T6 do not — T5 checks no
    status; T6's ordering keys on last_ingested_at, untouched by the UPDATE).

    Seeding one "done" row is not sufficient by itself: the module-scoped
    fixture accumulates T5's two "pending" rows and T6's rows across the
    file, so poll would still read True from those. The UPDATE with no
    WHERE clause is what actually clears every row to a terminal status.
    """
    _insert_document(
        documents_env.test_url,
        documents_env.uploader_id,
        original_filename="settled.pdf",
        ingestion_status="done",
    )

    async def _settle_all_rows() -> None:
        engine = create_async_engine(documents_env.test_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                # No WHERE clause — every row in the table, not just this one.
                # Document is not append-only (that constraint is AuditLog's
                # alone, CLAUDE.md §12); UPDATE over DELETE avoids disturbing
                # any document_image FK dependents.
                await session.execute(update(Document).values(ingestion_status="done"))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_settle_all_rows())

    response = _get(documents_env, documents_env.admin_session, headers={"accept": "text/html"})

    assert response.status_code == 200
    # Proves the list is non-empty AND unpolled — without this, the test could
    # pass vacuously off the empty-state branch, which omits the polling trigger
    # for an unrelated reason. M8c amendment: "unpolled" is "no every-Ns
    # trigger"; the event refresh trigger is always present, and asserting it
    # here keeps a regression that drops the refresh path from passing.
    assert "settled.pdf" in response.text
    assert "every" not in response.text
    assert "document-changed from:body" in response.text
