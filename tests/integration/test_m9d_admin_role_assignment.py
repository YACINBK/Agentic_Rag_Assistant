"""M9d admin role assignment — integration tests against real PostgreSQL.

This test verifies that an admin can change a user's primary role, that the
role_source is correctly set to 'admin_assigned', and that sessions are
purged if and only if the role actually changed.
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
from app.core.models.user import User, ROLE_SOURCE_ADMIN_ASSIGNED
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

SEED_ROLE_QA_ENGINEER_ID = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000002")

ADMIN_SESSION_ID = "admin-role-session"

@pytest.fixture(scope="module")
def role_env():
    """Migrate a test DB, seed one Admin plus two ordinary users, wire a plain app."""
    test_url = require_test_database_url("admin role assignment")

    admin_id = uuid.uuid4()
    target_a_id = uuid.uuid4()  # T1 — successful change
    target_b_id = uuid.uuid4()  # T2 — idempotent change

    admin_session = make_user_session(
        user_id=str(admin_id), email="admin@role.test", is_admin=True, is_owner=False
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
                            User(
                                id=admin_id,
                                email="admin@role.test",
                                keycloak_id="kc-role-admin",
                                role_id=SEED_ROLE_DEVELOPER_ID,
                                is_admin=True,
                                is_owner=False,
                            ),
                            User(
                                id=target_a_id,
                                email="target-a@role.test",
                                keycloak_id="kc-role-target-a",
                                role_id=SEED_ROLE_DEVELOPER_ID,
                                is_admin=False,
                                is_owner=False,
                            ),
                            User(
                                id=target_b_id,
                                email="target-b@role.test",
                                keycloak_id="kc-role-target-b",
                                role_id=SEED_ROLE_QA_ENGINEER_ID,
                                is_admin=False,
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
        redis._store[f"session:{ADMIN_SESSION_ID}"] = serialize_user_session(admin_session)
        redis._sets[user_sessions_key(str(admin_id))] = {ADMIN_SESSION_ID}

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
            admin_id=admin_id,
            target_a_id=target_a_id,
            target_b_id=target_b_id,
            admin_session=ADMIN_SESSION_ID,
        )

        app.dependency_overrides.clear()
        asyncio.run(route_engine.dispose())
        command.downgrade(cfg, "base")


def _request(env, method: str, url: str, session_id, data=None, **kwargs):
    """Issue one request with a clean cookie jar."""
    env.client.cookies.clear()
    if session_id is not None:
        env.client.cookies.set(SESSION_COOKIE, session_id)
    return env.client.request(method, url, data=data, **kwargs)


def _read_user(test_url: str, user_id: uuid.UUID) -> dict:
    """Snapshot user row."""
    async def _read() -> dict:
        engine = create_async_engine(test_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                row = (
                    await session.execute(select(User).where(User.id == user_id))
                ).scalar_one()
                return {
                    "role_id": row.role_id,
                    "role_source": row.role_source,
                }
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def _seed_sessions(env, user_id: uuid.UUID, session_ids: list[str]) -> None:
    """Give one user live sessions."""
    for session_id in session_ids:
        env.redis._store[f"session:{session_id}"] = serialize_user_session(
            make_user_session(user_id=str(user_id))
        )
    env.redis._sets[user_sessions_key(str(user_id))] = set(session_ids)


def test_successful_role_assignment(role_env) -> None:
    """T1 — Assertions 2, 3, 4."""
    _seed_sessions(role_env, role_env.target_a_id, ["s1", "s2"])
    assert "session:s1" in role_env.redis._store

    response = _request(
        role_env,
        "POST",
        f"/admin/users/{role_env.target_a_id}/role",
        role_env.admin_session,
        data={"role_id": str(SEED_ROLE_QA_ENGINEER_ID)},
    )

    assert response.status_code == 200
    assert "target-a@role.test" in response.text
    
    user = _read_user(role_env.test_url, role_env.target_a_id)
    assert user["role_id"] == SEED_ROLE_QA_ENGINEER_ID
    assert user["role_source"] == ROLE_SOURCE_ADMIN_ASSIGNED
    
    # Session purge
    assert "session:s1" not in role_env.redis._store
    assert "session:s2" not in role_env.redis._store
    assert role_env.redis._sets.get(user_sessions_key(str(role_env.target_a_id)), set()) == set()
    # Admin spared
    assert f"session:{role_env.admin_session}" in role_env.redis._store


def test_idempotent_role_assignment(role_env) -> None:
    """T2 — Assertion 5."""
    _seed_sessions(role_env, role_env.target_b_id, ["s3"])
    assert "session:s3" in role_env.redis._store

    response = _request(
        role_env,
        "POST",
        f"/admin/users/{role_env.target_b_id}/role",
        role_env.admin_session,
        data={"role_id": str(SEED_ROLE_QA_ENGINEER_ID)},
    )

    assert response.status_code == 200
    assert "session:s3" in role_env.redis._store
    assert role_env.redis._sets[user_sessions_key(str(role_env.target_b_id))] == {"s3"}


def test_unknown_user_is_404(role_env) -> None:
    """Output: Unknown user ID is 404."""
    missing = uuid.uuid4()
    response = _request(
        role_env,
        "POST",
        f"/admin/users/{missing}/role",
        role_env.admin_session,
        data={"role_id": str(SEED_ROLE_DEVELOPER_ID)},
    )
    assert response.status_code == 404


def test_unknown_role_is_404(role_env) -> None:
    """Output: Unknown role ID is 404."""
    missing_role = uuid.uuid4()
    response = _request(
        role_env,
        "POST",
        f"/admin/users/{role_env.target_a_id}/role",
        role_env.admin_session,
        data={"role_id": str(missing_role)},
    )
    assert response.status_code == 404


def test_privilege_violation_is_403(role_env) -> None:
    """Assertion 1: Non-admin cannot assign roles."""
    # Need a non-admin session
    non_admin_session = make_user_session(
        user_id=str(uuid.uuid4()), email="user@role.test", is_admin=False, is_owner=False
    )
    session_id = str(uuid.uuid4())
    role_env.redis._store[f"session:{session_id}"] = serialize_user_session(non_admin_session)
        
    response = _request(
        role_env,
        "POST",
        f"/admin/users/{role_env.target_a_id}/role",
        session_id,
        data={"role_id": str(SEED_ROLE_QA_ENGINEER_ID)},
    )
    # depends on error handler, usually 403 or redirect
    assert response.status_code in (403, 303)
