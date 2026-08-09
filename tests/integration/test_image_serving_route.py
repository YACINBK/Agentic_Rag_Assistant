"""Image serving route — integration tests against real PostgreSQL + filesystem.

The assertions are meaningless without a real database: the load-bearing one
(assertion 4, the cross-document bypass) is a property of the `document_image`
JOIN, and asserting it against a mocked session would assert the mock. So this
lives in the integration suite and is SKIPPED when TEST_DATABASE_URL is unset —
exactly like M1's migration tests (contract Notes).

Trap addressed: `app/core/models/base.py` builds its engine from
`settings.DATABASE_URL` at import time. Monkeypatching the setting does not rebind
it, so the route would read the dev database and 404 on every seeded row. The fix
is a dependency override of `get_session` pointing at a test-URL factory, with the
seed written through the same test URL — never a change to the app's import-time
plumbing (contract Environment).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.error_handlers import register_error_handlers
from app.api.routes import images_router
from app.core.models.base import get_session
from app.core.settings import settings
from tests.conftest import (
    MockRedis,
    make_image_bytes,
    make_user_session,
    seed_document_with_image,
    serialize_user_session,
)

USER_SESSION_ID = "img-user-session"
ADMIN_SESSION_ID = "img-admin-session"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture(scope="module")
def image_env(tmp_path_factory):
    """Migrate a test DB, seed two documents + images, and wire an app whose
    get_session reads that same test DB. NullPool everywhere so connections never
    outlive the loop that opened them."""
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL not set — image route integration tests skipped")
    if test_url == str(settings.DATABASE_URL):
        pytest.fail(
            "TEST_DATABASE_URL must NOT equal settings.DATABASE_URL — "
            "downgrade base would destroy the development database"
        )

    upload_dir = tmp_path_factory.mktemp("uploads")

    doc_open_id = uuid.uuid4()
    doc_locked_id = uuid.uuid4()
    bytes_a = make_image_bytes()
    bytes_b = make_image_bytes()
    image_a = _sha(bytes_a)
    image_b = _sha(bytes_b)
    # A pairing that exists in document_image but whose file is never written
    # (assertion 7).
    image_missing = _sha(make_image_bytes())

    with pytest.MonkeyPatch.context() as mp:
        # settings.DATABASE_URL: read by alembic/env.py at migration time.
        # settings.UPLOAD_DIR: read by the route at request time.
        mp.setattr(settings, "DATABASE_URL", test_url)
        mp.setattr(settings, "UPLOAD_DIR", str(upload_dir))

        cfg = Config("alembic.ini")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")

        async def _seed() -> None:
            engine = create_async_engine(test_url, poolclass=NullPool)
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session:
                await seed_document_with_image(
                    session,
                    doc_open_id,
                    image_a,
                    bytes_a,
                    restricted=False,
                    tmp_upload_dir=upload_dir,
                )
                await seed_document_with_image(
                    session,
                    doc_locked_id,
                    image_b,
                    bytes_b,
                    restricted=True,
                    tmp_upload_dir=upload_dir,
                )
                # Extra junction row on doc_open with no file on disk (assertion 7).
                from app.core.models.document_image import DocumentImage

                session.add(DocumentImage(document_id=doc_open_id, image_id=image_missing))
                await session.commit()
            await engine.dispose()

        asyncio.run(_seed())

        # The route's own engine on the test URL — the import-time engine points at
        # the dev DB, so the override is the seam (contract trap).
        route_engine = create_async_engine(test_url, poolclass=NullPool)
        route_factory = async_sessionmaker(
            route_engine, class_=AsyncSession, expire_on_commit=False
        )

        async def _override_get_session():
            async with route_factory() as session:
                yield session

        redis = MockRedis()
        redis._store[f"session:{USER_SESSION_ID}"] = serialize_user_session(
            make_user_session(is_admin=False)
        )
        redis._store[f"session:{ADMIN_SESSION_ID}"] = serialize_user_session(
            make_user_session(is_admin=True)
        )

        app = FastAPI()
        app.state.redis = redis
        register_error_handlers(app)
        app.include_router(images_router)
        app.dependency_overrides[get_session] = _override_get_session

        client = TestClient(app)

        yield SimpleNamespace(
            client=client,
            doc_open=doc_open_id,
            doc_locked=doc_locked_id,
            image_a=image_a,
            image_b=image_b,
            image_missing=image_missing,
            bytes_a=bytes_a,
            bytes_b=bytes_b,
        )

        app.dependency_overrides.clear()
        asyncio.run(route_engine.dispose())
        command.downgrade(cfg, "base")


def _get(env, session_id, doc_id, image_id):
    env.client.cookies.clear()
    if session_id is not None:
        env.client.cookies.set("session_id", session_id)
    return env.client.get(f"/documents/{doc_id}/images/{image_id}")


def test_unrestricted_image_served_to_normal_user(image_env):
    """Assertions 1, 8: non-admin gets 200, exact bytes, Cache-Control private."""
    response = _get(image_env, USER_SESSION_ID, image_env.doc_open, image_env.image_a)

    assert response.status_code == 200
    assert response.content == image_env.bytes_a
    cache_control = response.headers["cache-control"].lower()
    assert "private" in cache_control
    assert "public" not in cache_control


def test_restricted_image_denied_to_normal_user(image_env):
    """Assertion 2: non-admin gets 403 for a restricted document's image."""
    response = _get(image_env, USER_SESSION_ID, image_env.doc_locked, image_env.image_b)

    assert response.status_code == 403


def test_restricted_image_served_to_admin(image_env):
    """Assertion 3: admin gets 200 for that same restricted image."""
    response = _get(image_env, ADMIN_SESSION_ID, image_env.doc_locked, image_env.image_b)

    assert response.status_code == 200
    assert response.content == image_env.bytes_b


def test_cross_document_pairing_is_404(image_env):
    """Assertion 4 — the bypass. A valid doc_id paired with an image from a
    different document is 404 in BOTH directions, restricted or not."""
    # readable document + image belonging to the restricted one
    r1 = _get(image_env, USER_SESSION_ID, image_env.doc_open, image_env.image_b)
    assert r1.status_code == 404

    # restricted document + image belonging to the readable one
    r2 = _get(image_env, USER_SESSION_ID, image_env.doc_locked, image_env.image_a)
    assert r2.status_code == 404


def test_unauthenticated_request_rejected(image_env):
    """Assertion 5: no session cookie never reaches the handler — the same
    AuthenticationError every protected route raises (401 for a non-HTML client)."""
    response = _get(image_env, None, image_env.doc_open, image_env.image_a)

    assert response.status_code == 401


def test_traversal_image_id_is_404(image_env):
    """Assertion 6: a malformed image_id is 404 and reads no file outside derived/.

    Two layers, and the distinction matters. No slash-bearing image_id can reach
    the handler at all — Starlette's router rejects `../../../etc/passwd`,
    `..%2f..%2fpasswd` and `%2e%2e%2f...` before any dependency runs, because none
    of them match a single `{image_id}` path segment. Asserting only those makes
    this test a tautology: it would still pass with the hex guard deleted.

    So the routable cases are asserted too. They are what actually exercise
    `_IMAGE_ID_RE`, and glob metacharacters are the guard's real job — `glob()`
    treats `*` and `?` as a pattern, not a literal, so an unguarded image_id is a
    wildcard search over derived/ rather than a lookup.
    """
    # Layer 1 — blocked at routing (never reaches the handler)
    for bad in ("../../../etc/passwd", "..%2f..%2fpasswd", "%2e%2e%2f%2e%2e%2fpasswd"):
        response = _get(image_env, USER_SESSION_ID, image_env.doc_open, bad)
        assert response.status_code == 404, bad

    # Layer 2 — routable, so the hex guard is what rejects these
    for bad in ("abc", "A" * 64, "g" * 64, "*" * 64, "?" * 64):
        response = _get(image_env, USER_SESSION_ID, image_env.doc_open, bad)
        assert response.status_code == 404, bad


def test_missing_file_is_404_not_500(image_env):
    """Assertion 7: a pairing that exists in document_image but whose file is
    absent from disk returns 404, not 500."""
    response = _get(image_env, USER_SESSION_ID, image_env.doc_open, image_env.image_missing)

    assert response.status_code == 404
