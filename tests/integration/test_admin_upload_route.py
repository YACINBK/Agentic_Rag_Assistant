"""Admin upload route (M7) — integration tests against real PostgreSQL + filesystem.

Why integration, not unit: the load-bearing behaviors are DB properties. Content
dedup (409) is the `Document.doc_hash` UNIQUE lookup, and "persists" is a real row
with a real `uploaded_by` FK into `user`. Asserting those against a mocked session
would assert the mock. So this lives in the integration suite and is SKIPPED when
TEST_DATABASE_URL is unset — the same posture as the M1 migration tests and the
image-serving route tests.

Trap addressed (identical to test_image_serving_route.py): `app/core/models/base.py`
builds its engine from `settings.DATABASE_URL` at import time. Monkeypatching the
setting does not rebind it, so the route would read the dev database. The fix is a
`get_session` dependency override pointing at a test-URL factory, with the seed
written through that same URL.

Two things are patched at the module boundary rather than reached for real:
- `app.api.routes.admin.ingest_document` — its `.delay()` would hit a live Celery
  broker. Patched so the enqueue is observable without a broker.
- (413 test only) `app.api.routes.admin.compute_doc_hash` — patched purely to
  assert it is NOT called, proving the size ceiling rejects before hashing.

The route's storage runs for real: `settings.UPLOAD_DIR` is monkeypatched to a tmp
directory and files land there, so the happy path exercises the true save + the
content-hash filename.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette_csrf import CSRFMiddleware

from alembic import command
from app.api.dependencies import SESSION_COOKIE
from app.api.error_handlers import register_error_handlers
from app.api.routes import admin_router
from app.core.models.base import get_session
from app.core.models.document import Document
from app.core.services.storage import compute_doc_hash
from app.core.settings import settings
from tests.conftest import (
    SEED_ROLE_DEVELOPER_ID,
    MockRedis,
    make_user_session,
    require_test_database_url,
    serialize_user_session,
)

pytestmark = pytest.mark.integration

ADMIN_SESSION_ID = "admin-upload-session"
NONADMIN_SESSION_ID = "nonadmin-upload-session"

INGEST_TASK = "app.api.routes.admin.ingest_document"
HASH_FN = "app.api.routes.admin.compute_doc_hash"


def _fetch_document(test_url: str, document_id: str) -> Document | None:
    """Read one Document by id through a fresh NullPool engine on the test URL."""

    async def _q() -> Document | None:
        engine = create_async_engine(test_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                result = await session.execute(
                    select(Document).where(Document.id == uuid.UUID(document_id))
                )
                return result.scalar_one_or_none()
        finally:
            await engine.dispose()

    return asyncio.run(_q())


def _raw_multipart(
    parts: list[tuple[str, str | None, bytes]], boundary: str = "M7BOUNDARY"
) -> bytes:
    """Build a multipart/form-data body by hand.

    Each part is (name, filename, data), where `filename=None` emits a plain form
    FIELD and any str — including "" — emits a FILE part.

    Both halves are load-bearing. httpx's `files={}` API drops an empty filename
    entirely, which demotes the part to a form field and makes the route's
    `if not file.filename` guard unreachable, so `filename=""` has to be written
    verbatim. But the disposition must be omitted for non-file parts: Starlette's
    multipart parser keys on the mere PRESENCE of `filename` (`if b"filename" in
    options`), so emitting `filename=""` on `category` binds it as an UploadFile
    rather than a str, and FastAPI's `Literal` check then rejects it with a 422
    (`literal_error`, input `{"filename": "", "file": ...}`) before the route body
    ever runs — a 422 where this test asserts 400.
    """
    lines: list[bytes] = []
    for name, filename, data in parts:
        lines.append(f"--{boundary}".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        lines.append(disposition.encode())
        if filename is not None:
            lines.append(b"Content-Type: application/octet-stream")
        lines.append(b"")
        lines.append(data)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    return b"\r\n".join(lines)


@pytest.fixture(scope="module")
def upload_env(tmp_path_factory):
    """Migrate a test DB, seed one admin uploader, and wire two apps whose
    get_session reads that same DB: a primary app with no CSRF middleware (tests
    1–7) and a CSRF-wrapped app (test 8). NullPool everywhere."""
    test_url = require_test_database_url("admin upload")

    upload_dir = tmp_path_factory.mktemp("uploads")

    admin_user = make_user_session(is_admin=True)
    nonadmin_user = make_user_session(is_admin=False)
    admin_user_id = uuid.UUID(admin_user.user_id)

    with pytest.MonkeyPatch.context() as mp:
        # DATABASE_URL: read by alembic/env.py at migration time.
        # UPLOAD_DIR: read by LocalFileStorage at request time.
        mp.setattr(settings, "DATABASE_URL", test_url)
        mp.setattr(settings, "UPLOAD_DIR", str(upload_dir))

        cfg = Config("alembic.ini")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")

        async def _seed() -> None:
            engine = create_async_engine(test_url, poolclass=NullPool)
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            try:
                async with factory() as session:
                    from app.core.models.user import User

                    # Only the admin needs a real row: it's the id that becomes
                    # Document.uploaded_by (a NOT NULL FK). The non-admin session
                    # is rejected at require_admin, long before any DB write.
                    session.add(
                        User(
                            id=admin_user_id,
                            email="admin-upload@seed.test",
                            keycloak_id="kc-admin-upload",
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

        # Primary app — no CSRF middleware. Tests 1–7 exercise the route directly.
        app = FastAPI()
        app.state.redis = redis
        register_error_handlers(app)
        app.include_router(admin_router)
        app.dependency_overrides[get_session] = _override_get_session
        client = TestClient(app)

        # CSRF-wrapped app — only test 8. Middleware configured exactly as main.py.
        # A /health endpoint gives test 8 a safe GET to mint the csrftoken cookie.
        csrf_app = FastAPI()
        csrf_app.state.redis = redis
        register_error_handlers(csrf_app)
        csrf_app.add_middleware(
            CSRFMiddleware,
            secret=settings.SECRET_KEY,
            sensitive_cookies={SESSION_COOKIE},
            # Compiled, matching main.py. starlette_csrf calls `.match()` on each
            # entry, so a plain str is an AttributeError on every unsafe request.
            exempt_urls=[re.compile(r"^/auth/callback$")],
        )
        csrf_app.include_router(admin_router)

        @csrf_app.get("/health")
        async def _health():
            return {"status": "ok"}

        csrf_app.dependency_overrides[get_session] = _override_get_session
        csrf_client = TestClient(csrf_app)

        yield SimpleNamespace(
            client=client,
            csrf_client=csrf_client,
            test_url=test_url,
            upload_dir=upload_dir,
            admin_session=ADMIN_SESSION_ID,
            admin_user_id=str(admin_user_id),
            nonadmin_session=NONADMIN_SESSION_ID,
        )

        app.dependency_overrides.clear()
        csrf_app.dependency_overrides.clear()
        asyncio.run(route_engine.dispose())
        command.downgrade(cfg, "base")


def _post(env, session_id, *, files=None, data=None, content=None, headers=None):
    """POST /admin/documents on the primary (no-CSRF) client with a clean cookie jar."""
    env.client.cookies.clear()
    if session_id is not None:
        env.client.cookies.set(SESSION_COOKIE, session_id)
    return env.client.post(
        "/admin/documents",
        files=files,
        data=data,
        content=content,
        headers=headers or {},
    )


def test_valid_upload_returns_202_persists_and_enqueues(upload_env):
    """Assertion — a valid admin upload returns 202 with a document_id, writes a
    Document row (correct category/restricted/uploader) plus a file on disk, and
    enqueues ingestion once with ["all"]."""
    payload = b"m7 valid upload unique payload -- test 1"
    with patch(INGEST_TASK) as mock_task:
        resp = _post(
            upload_env,
            upload_env.admin_session,
            files={"file": ("notes.txt", payload, "text/plain")},
            data={"category": "technical", "restricted": "true"},
        )

    assert resp.status_code == 202, resp.text
    document_id = resp.json()["document_id"]
    uuid.UUID(document_id)  # well-formed UUID

    # Enqueued exactly once, with the returned id and the hardcoded ["all"] scope.
    mock_task.delay.assert_called_once_with(document_id, ["all"])

    # Persisted with the fields the request carried.
    row = _fetch_document(upload_env.test_url, document_id)
    assert row is not None
    assert row.category == "technical"
    assert row.restricted is True
    assert str(row.uploaded_by) == upload_env.admin_user_id
    assert row.doc_hash == compute_doc_hash(payload)
    # The state the Celery orchestrator and the M8 document list both key off.
    # The route never sets it — it comes from the Python-side default on
    # app/core/models/document.py, and the initial migration declares the enum
    # with no server_default, so this is the only place in the repo that observes
    # it. A changed default string would otherwise pass every existing test.
    assert row.ingestion_status == "pending"

    # File written under UPLOAD_DIR with the content-hash name.
    assert (upload_env.upload_dir / f"{compute_doc_hash(payload)}.txt").exists()


def test_htmx_upload_returns_polling_list_partial(upload_env):
    """D28 — an HTMX upload must come back as the document-list partial, already
    carrying its own self-refresh trigger, so the new row animates pending →
    running → done with no manual reload.

    Two halves, both load-bearing:
    - HX-Request: true → the list partial (not JSON), status still 202, the
      just-uploaded filename present, and `hx-trigger` present because the fresh
      row is `pending` (M8 Assertion 7 — presence still determined solely by
      IN_FLIGHT_STATUSES, never made unconditional).
    - no HX-Request → unchanged JSON {"document_id": ...}, proving the branch is
      additive and the M7 API contract still holds.
    """
    with patch(INGEST_TASK) as mock_task:
        hx = _post(
            upload_env,
            upload_env.admin_session,
            files={"file": ("hx-upload.txt", b"d28 htmx branch payload", "text/plain")},
            data={"category": "technical", "restricted": "false"},
            headers={"HX-Request": "true"},
        )

    assert hx.status_code == 202, hx.text
    mock_task.delay.assert_called_once()

    # The list partial, not JSON and not a full page.
    assert "<html" not in hx.text
    assert 'id="document-list"' in hx.text
    assert "hx-upload.txt" in hx.text
    # Present because the row just committed is `pending` — the whole point of
    # D28. Without the swap it renders, the user sees a stale, static list.
    assert "hx-trigger" in hx.text
    assert "pending" in hx.text

    # The non-HTMX branch is untouched: still the M7 JSON contract.
    with patch(INGEST_TASK):
        plain = _post(
            upload_env,
            upload_env.admin_session,
            files={"file": ("plain-upload.txt", b"d28 json branch payload", "text/plain")},
            data={"category": "technical", "restricted": "false"},
        )

    assert plain.status_code == 202, plain.text
    uuid.UUID(plain.json()["document_id"])


def test_duplicate_content_returns_409(upload_env):
    """Assertion — re-uploading identical bytes returns 409 with the existing
    document_id and does not enqueue a second ingestion."""
    payload = b"m7 duplicate content payload -- test 2"

    with patch(INGEST_TASK) as first_task:
        first = _post(
            upload_env,
            upload_env.admin_session,
            files={"file": ("dup.txt", payload, "text/plain")},
            data={"category": "quality", "restricted": "false"},
        )
    assert first.status_code == 202, first.text
    first_id = first.json()["document_id"]
    first_task.delay.assert_called_once()

    with patch(INGEST_TASK) as second_task:
        dup = _post(
            upload_env,
            upload_env.admin_session,
            files={"file": ("dup-again.txt", payload, "text/plain")},
            data={"category": "quality", "restricted": "false"},
        )

    assert dup.status_code == 409, dup.text
    body = dup.json()
    assert body["document_id"] == first_id
    # The duplicate never reached the enqueue.
    second_task.delay.assert_not_called()


def test_bad_filename_returns_400_empty_and_disallowed_extension(upload_env):
    """Assertion — a missing filename and a disallowed extension both return 400
    (never 422/500), and neither reaches the ingestion enqueue."""
    # (a) empty filename: hand-built multipart with filename="" so it binds as an
    # UploadFile whose .filename is "" and trips the route's filename guard.
    # `category` passes filename=None — it must stay a plain field, or it binds as
    # an UploadFile and FastAPI 422s on the Literal before the guard is reached.
    body = _raw_multipart([("file", "", b"no filename here"), ("category", None, b"technical")])
    with patch(INGEST_TASK) as task_a:
        resp_a = _post(
            upload_env,
            upload_env.admin_session,
            content=body,
            headers={"content-type": "multipart/form-data; boundary=M7BOUNDARY"},
        )
    assert resp_a.status_code == 400, resp_a.text
    task_a.delay.assert_not_called()

    # (b) disallowed extension: passes the filename guard, then storage.save
    # raises FileValidationError, caught locally as 400.
    with patch(INGEST_TASK) as task_b:
        resp_b = _post(
            upload_env,
            upload_env.admin_session,
            files={
                "file": (
                    "evil.exe",
                    b"m7 bad-extension payload -- test 3b",
                    "application/octet-stream",
                )
            },
            data={"category": "technical", "restricted": "false"},
        )
    assert resp_b.status_code == 400, resp_b.text
    task_b.delay.assert_not_called()


def test_oversized_upload_returns_413_before_hashing(upload_env):
    """Assertion — a body over MAX_UPLOAD_SIZE_BYTES returns 413 and is rejected
    before the content is hashed or enqueued."""
    with pytest.MonkeyPatch.context() as mp:
        # Read at the call site, so the low ceiling applies to this request only.
        mp.setattr(settings, "MAX_UPLOAD_SIZE_BYTES", 8)
        with patch(HASH_FN) as mock_hash, patch(INGEST_TASK) as mock_task:
            resp = _post(
                upload_env,
                upload_env.admin_session,
                files={
                    "file": (
                        "big.txt",
                        b"this body is definitely longer than eight bytes",
                        "text/plain",
                    )
                },
                data={"category": "technical", "restricted": "false"},
            )

    assert resp.status_code == 413, resp.text
    # The whole point of "before hashing": the hash function was never called.
    mock_hash.assert_not_called()
    mock_task.delay.assert_not_called()


def test_non_admin_session_returns_403(upload_env):
    """Assertion — a valid non-admin session is rejected with 403; primary role
    alone never grants upload."""
    with patch(INGEST_TASK) as mock_task:
        resp = _post(
            upload_env,
            upload_env.nonadmin_session,
            files={"file": ("x.txt", b"m7 non-admin payload -- test 5", "text/plain")},
            data={"category": "technical", "restricted": "false"},
            headers={"accept": "application/json"},
        )

    assert resp.status_code == 403, resp.text
    mock_task.delay.assert_not_called()


def test_unauthenticated_request_returns_401(upload_env):
    """Assertion — no session cookie is rejected with 401 before the handler."""
    with patch(INGEST_TASK) as mock_task:
        resp = _post(
            upload_env,
            None,
            files={"file": ("x.txt", b"m7 unauth payload -- test 6", "text/plain")},
            data={"category": "technical", "restricted": "false"},
            headers={"accept": "application/json"},
        )

    assert resp.status_code == 401, resp.text
    mock_task.delay.assert_not_called()


def test_invalid_category_returns_422(upload_env):
    """Assertion — a category outside the four allowed values is rejected with 422
    by request validation (admin session, so auth is not the failing gate)."""
    with patch(INGEST_TASK) as mock_task:
        resp = _post(
            upload_env,
            upload_env.admin_session,
            files={"file": ("x.txt", b"m7 invalid category payload -- test 7", "text/plain")},
            data={"category": "marketing", "restricted": "false"},
        )

    assert resp.status_code == 422, resp.text
    mock_task.delay.assert_not_called()


def test_csrf_missing_header_rejected_matching_pair_succeeds(upload_env):
    """Assertion — with CSRF middleware active, a POST bearing the session cookie
    but no matching csrftoken header is rejected 403, while the same POST with a
    matching cookie+header pair passes CSRF and reaches the route (202)."""
    csrf_client = upload_env.csrf_client

    # Mint a csrftoken cookie via a safe GET; the middleware sets it on the first
    # response that arrives without one.
    csrf_client.cookies.clear()
    health = csrf_client.get("/health")
    assert health.status_code == 200
    csrf_token = csrf_client.cookies.get("csrftoken")
    assert csrf_token is not None

    # (a) session cookie + csrftoken cookie, but NO x-csrftoken header → 403.
    csrf_client.cookies.clear()
    csrf_client.cookies.set(SESSION_COOKIE, upload_env.admin_session)
    csrf_client.cookies.set("csrftoken", csrf_token)
    with patch(INGEST_TASK) as task_a:
        resp_a = csrf_client.post(
            "/admin/documents",
            files={"file": ("c.txt", b"m7 csrf missing header -- test 8a", "text/plain")},
            data={"category": "technical", "restricted": "false"},
        )
    assert resp_a.status_code == 403, resp_a.text
    task_a.delay.assert_not_called()

    # (b) same cookies + a matching x-csrftoken header → CSRF passes → 202.
    csrf_client.cookies.clear()
    csrf_client.cookies.set(SESSION_COOKIE, upload_env.admin_session)
    csrf_client.cookies.set("csrftoken", csrf_token)
    with patch(INGEST_TASK) as task_b:
        resp_b = csrf_client.post(
            "/admin/documents",
            files={"file": ("c.txt", b"m7 csrf matching pair -- test 8b", "text/plain")},
            data={"category": "technical", "restricted": "false"},
            headers={"x-csrftoken": csrf_token},
        )
    assert resp_b.status_code == 202, resp_b.text
    task_b.delay.assert_called_once()
