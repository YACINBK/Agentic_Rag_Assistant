"""M9 admin users UI — integration tests against real PostgreSQL.

What needs the database: the owner-only 403 branch, the full-page/partial split,
the grant/revoke writes, the `ck_owner_implies_admin` refusal (assertion 6 cannot
be mocked — the constraint is the whole point), and the "every other column
byte-identical" guard (assertion 9), which is a live regression test for D6's
`onupdate=func.now()` hazard on `last_login`.

Fixture follows tests/integration/test_m8_admin_documents_ui.py's documents_env.
No CSRFMiddleware: the M9 contract's Environment table records CSRF as inherited
infrastructure already proven by test_admin_upload_route.py, not re-asserted here.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.dependencies import SESSION_COOKIE
from app.api.error_handlers import register_error_handlers
from app.api.routes import admin_router
from app.core.models.base import get_session
from app.core.models.user import User
from app.core.settings import settings
from app.services.sessions import user_sessions_key
from tests.conftest import (
    SEED_ROLE_DEVELOPER_ID,
    MockRedis,
    make_user_session,
    require_test_database_url,
    serialize_user_session,
)

pytestmark = pytest.mark.integration

# Both roles are seeded by the initial migration
# (alembic/versions/ddb267266304_initial_schema.py:26-27). Two distinct roles are
# used so `role.name` rendering is proven against the table, not a constant.
SEED_ROLE_QA_ENGINEER_ID = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000002")

OWNER_SESSION_ID = "owner-users-session"
ADMIN_ONLY_SESSION_ID = "adminonly-users-session"


@pytest.fixture(scope="module")
def users_env():
    """Migrate a test DB, seed one Owner plus four ordinary users, wire a plain app."""
    test_url = require_test_database_url("admin users ui")

    owner_id = uuid.uuid4()
    admin_only_id = uuid.uuid4()
    target_a_id = uuid.uuid4()  # T4 — grant/revoke round trip
    target_b_id = uuid.uuid4()  # T6 — real change purges sessions
    target_c_id = uuid.uuid4()  # T7 — idempotent grant purges nothing

    owner_session = make_user_session(
        user_id=str(owner_id), email="owner@users.test", is_admin=True, is_owner=True
    )
    admin_only_session = make_user_session(
        user_id=str(admin_only_id), email="adminonly@users.test", is_admin=True, is_owner=False
    )

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
                    session.add_all(
                        [
                            # Exactly one is_owner row — idx_single_owner is a
                            # partial UNIQUE index, a second would fail to insert.
                            User(
                                id=owner_id,
                                email="owner@users.test",
                                keycloak_id="kc-users-owner",
                                role_id=SEED_ROLE_DEVELOPER_ID,
                                is_admin=True,
                                is_owner=True,
                            ),
                            User(
                                id=admin_only_id,
                                email="adminonly@users.test",
                                keycloak_id="kc-users-adminonly",
                                role_id=SEED_ROLE_QA_ENGINEER_ID,
                                is_admin=True,
                                is_owner=False,
                            ),
                            User(
                                id=target_a_id,
                                email="target-a@users.test",
                                keycloak_id="kc-users-target-a",
                                role_id=SEED_ROLE_DEVELOPER_ID,
                                is_admin=False,
                                is_owner=False,
                            ),
                            User(
                                id=target_b_id,
                                email="target-b@users.test",
                                keycloak_id="kc-users-target-b",
                                role_id=SEED_ROLE_DEVELOPER_ID,
                                is_admin=False,
                                is_owner=False,
                            ),
                            User(
                                id=target_c_id,
                                email="target-c@users.test",
                                keycloak_id="kc-users-target-c",
                                role_id=SEED_ROLE_QA_ENGINEER_ID,
                                is_admin=True,
                                is_owner=False,
                            ),
                        ]
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
        redis._store[f"session:{OWNER_SESSION_ID}"] = serialize_user_session(owner_session)
        redis._store[f"session:{ADMIN_ONLY_SESSION_ID}"] = serialize_user_session(
            admin_only_session
        )
        # The actor's own session is indexed too, so a buggy purge that ignored
        # the target id would be caught by T6 rather than passing silently.
        redis._sets[user_sessions_key(str(owner_id))] = {OWNER_SESSION_ID}

        app = FastAPI()
        app.state.redis = redis
        register_error_handlers(app)
        app.include_router(admin_router)
        app.dependency_overrides[get_session] = _override_get_session
        client = TestClient(app)

        yield SimpleNamespace(
            client=client,
            redis=redis,
            test_url=test_url,
            owner_id=owner_id,
            admin_only_id=admin_only_id,
            target_a_id=target_a_id,
            target_b_id=target_b_id,
            target_c_id=target_c_id,
            owner_session=OWNER_SESSION_ID,
            admin_only_session=ADMIN_ONLY_SESSION_ID,
        )

        app.dependency_overrides.clear()
        asyncio.run(route_engine.dispose())
        command.downgrade(cfg, "base")


def _request(env, method: str, url: str, session_id, **kwargs):
    """Issue one request with a clean cookie jar."""
    env.client.cookies.clear()
    if session_id is not None:
        env.client.cookies.set(SESSION_COOKIE, session_id)
    return env.client.request(method, url, **kwargs)


def _read_user_row(test_url: str, user_id: uuid.UUID) -> dict:
    """Snapshot every column of one user row."""

    async def _read() -> dict:
        engine = create_async_engine(test_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                row = (
                    await session.execute(select(User).where(User.id == user_id))
                ).scalar_one()
                return {
                    "id": row.id,
                    "email": row.email,
                    "keycloak_id": row.keycloak_id,
                    "role_id": row.role_id,
                    "is_admin": row.is_admin,
                    "is_owner": row.is_owner,
                    "created_at": row.created_at,
                    "last_login": row.last_login,
                }
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def _seed_sessions(env, user_id: uuid.UUID, session_ids: list[str]) -> None:
    """Give one user live sessions plus the reverse-index set entry.

    Seeded per test rather than in the fixture so no test depends on another
    having (or not having) already purged these keys.
    """
    for session_id in session_ids:
        env.redis._store[f"session:{session_id}"] = serialize_user_session(
            make_user_session(user_id=str(user_id))
        )
    env.redis._sets[user_sessions_key(str(user_id))] = set(session_ids)


def test_full_page_vs_partial(users_env) -> None:
    """T2 — Assertions 1, 2."""
    full = _request(
        users_env, "GET", "/admin/users", users_env.owner_session, headers={"accept": "text/html"}
    )
    partial = _request(
        users_env, "GET", "/admin/users", users_env.owner_session, headers={"HX-Request": "true"}
    )

    assert full.status_code == 200
    assert "<html" in full.text
    assert "Whitecape Knowledge" in full.text
    assert "owner@users.test" in full.text
    assert "target-a@users.test" in full.text

    assert partial.status_code == 200
    assert "<html" not in partial.text
    assert "Whitecape Knowledge" not in partial.text
    assert "owner@users.test" in partial.text
    assert "target-a@users.test" in partial.text
    # Both seeded roles render, so role.name comes from the Role table.
    assert "developer" in partial.text
    assert "qa_engineer" in partial.text


def test_authorization_is_owner_only(users_env) -> None:
    """T3 — Assertion 3. `accept: application/json` pins the JSON branch of
    register_error_handlers so an HTML redirect cannot be misread as a pass."""
    owner = _request(
        users_env,
        "GET",
        "/admin/users",
        users_env.owner_session,
        headers={"accept": "application/json"},
    )
    admin_not_owner = _request(
        users_env,
        "GET",
        "/admin/users",
        users_env.admin_only_session,
        headers={"accept": "application/json"},
    )
    unauthenticated = _request(
        users_env, "GET", "/admin/users", None, headers={"accept": "application/json"}
    )

    assert owner.status_code == 200
    # The assertion that makes this owner-only rather than admin-only.
    assert admin_not_owner.status_code == 403
    assert unauthenticated.status_code == 401


def test_grant_then_revoke_round_trip(users_env) -> None:
    """T4 — Assertions 4, 5, 9, and Forbidden 3 (no JSON from a mutation)."""
    before = _read_user_row(users_env.test_url, users_env.target_a_id)
    assert before["is_admin"] is False

    granted = _request(
        users_env, "POST", f"/admin/users/{users_env.target_a_id}/admin", users_env.owner_session
    )
    after_grant = _read_user_row(users_env.test_url, users_env.target_a_id)

    assert granted.status_code == 200
    assert granted.headers["content-type"].startswith("text/html")
    assert "target-a@users.test" in granted.text
    assert "<html" not in granted.text  # a row fragment, not a page
    assert after_grant["is_admin"] is True

    revoked = _request(
        users_env, "DELETE", f"/admin/users/{users_env.target_a_id}/admin", users_env.owner_session
    )
    after_revoke = _read_user_row(users_env.test_url, users_env.target_a_id)

    assert revoked.status_code == 200
    assert revoked.headers["content-type"].startswith("text/html")
    assert after_revoke["is_admin"] is False

    # Assertion 9 — every other column untouched by both writes. last_login is
    # the one that would break if onupdate=func.now() came back (D6).
    for column in ("id", "email", "keycloak_id", "role_id", "is_owner", "created_at", "last_login"):
        assert after_grant[column] == before[column], column
        assert after_revoke[column] == before[column], column


def test_revoke_against_the_owner_is_refused_not_crashed(users_env) -> None:
    """T5 — Assertion 6. Exactly 409: a 500 would mean ck_owner_implies_admin
    raised an IntegrityError instead of the route refusing in application code."""
    before = _read_user_row(users_env.test_url, users_env.owner_id)

    response = _request(
        users_env, "DELETE", f"/admin/users/{users_env.owner_id}/admin", users_env.owner_session
    )
    after = _read_user_row(users_env.test_url, users_env.owner_id)

    assert response.status_code == 409
    assert after["is_admin"] is True
    assert after["is_owner"] is True
    assert before == after


def test_real_flag_change_purges_target_sessions_and_spares_the_actor(users_env) -> None:
    """T6 — Assertions 7, 8."""
    _seed_sessions(users_env, users_env.target_b_id, ["t1", "t2"])
    assert "session:t1" in users_env.redis._store

    response = _request(
        users_env, "POST", f"/admin/users/{users_env.target_b_id}/admin", users_env.owner_session
    )

    assert response.status_code == 200
    assert _read_user_row(users_env.test_url, users_env.target_b_id)["is_admin"] is True
    # Assertion 7 — the stored is_admin the target's sessions carry is now wrong,
    # so those sessions are gone rather than left holding §5 scope they lost.
    assert "session:t1" not in users_env.redis._store
    assert "session:t2" not in users_env.redis._store
    assert users_env.redis._sets.get(user_sessions_key(str(users_env.target_b_id)), set()) == set()
    # Assertion 8 — the actor is still logged in.
    assert f"session:{users_env.owner_session}" in users_env.redis._store
    assert users_env.redis._sets[user_sessions_key(str(users_env.owner_id))] == {
        users_env.owner_session
    }


def test_idempotent_grant_purges_nothing(users_env) -> None:
    """T7 — Forbidden 7. The case that separates "the flag changed" from "the
    route was called", and the one a naive implementation gets wrong."""
    before = _read_user_row(users_env.test_url, users_env.target_c_id)
    assert before["is_admin"] is True
    _seed_sessions(users_env, users_env.target_c_id, ["c1", "c2"])

    response = _request(
        users_env, "POST", f"/admin/users/{users_env.target_c_id}/admin", users_env.owner_session
    )

    assert response.status_code == 200
    assert "session:c1" in users_env.redis._store
    assert "session:c2" in users_env.redis._store
    assert users_env.redis._sets[user_sessions_key(str(users_env.target_c_id))] == {"c1", "c2"}
    assert _read_user_row(users_env.test_url, users_env.target_c_id) == before


def test_unknown_user_id_is_404(users_env) -> None:
    """Outputs table — unknown id is a 404 from HTTPException, not a 500."""
    missing = uuid.uuid4()

    granted = _request(users_env, "POST", f"/admin/users/{missing}/admin", users_env.owner_session)
    revoked = _request(
        users_env, "DELETE", f"/admin/users/{missing}/admin", users_env.owner_session
    )

    assert granted.status_code == 404
    assert revoked.status_code == 404


def test_no_route_wrote_is_owner(users_env) -> None:
    """Assertion 10. Order-independent: whatever the suite did, there is still
    exactly one Owner and it is the seeded row."""

    async def _read_owners() -> list[uuid.UUID]:
        engine = create_async_engine(users_env.test_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                rows = (
                    await session.execute(select(User).where(User.is_owner.is_(True)))
                ).scalars().all()
                return [row.id for row in rows]
        finally:
            await engine.dispose()

    assert asyncio.run(_read_owners()) == [users_env.owner_id]
