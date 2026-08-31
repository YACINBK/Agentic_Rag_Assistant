from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import main as main_module
from app.api.dependencies import require_auth
from app.api.error_handlers import register_error_handlers
from app.api.routes.onboarding import onboarding_router
from app.core.models.base import get_session
from app.core.models.role import Role
from app.core.models.user import User
from app.core.security import UserSession
from app.core.settings import settings
from tests.conftest import MockRedis, require_test_database_url

pytestmark = pytest.mark.integration


class MockKeycloakAdmin:
    """Programmatic identity double for the callback boundary."""

    user_session: UserSession

    def __init__(self, db, redis: MockRedis):
        self.redis = redis

    async def handle_callback(self, request: Request) -> UserSession:
        session_id = f"callback-{uuid.uuid4().hex}"
        request.state.session_id = session_id
        await self.redis.setex(
            f"session:{session_id}",
            settings.SESSION_TTL_SECONDS,
            json.dumps(self.user_session.__dict__),
        )
        return self.user_session


@pytest.fixture(scope="module")
def role_picker_env():
    test_url = require_test_database_url("M9c role picker")
    default_role_id = uuid.uuid4()
    selected_role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    default_role_name = f"scope-{uuid.uuid4().hex}"
    selected_role_name = f"scope-{uuid.uuid4().hex}"

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(settings, "DATABASE_URL", test_url)
        config = Config("alembic.ini")
        command.downgrade(config, "base")
        command.upgrade(config, "head")

        async def seed() -> None:
            engine = create_async_engine(test_url, poolclass=NullPool)
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            try:
                async with factory() as session:
                    session.add_all(
                        [
                            Role(id=default_role_id, name=default_role_name),
                            Role(id=selected_role_id, name=selected_role_name),
                            User(
                                id=user_id,
                                email=f"picker-{user_id}@test.invalid",
                                keycloak_id=f"kc-{user_id}",
                                role_id=default_role_id,
                                role_source="default",
                            ),
                        ]
                    )
                    await session.commit()
            finally:
                await engine.dispose()

        asyncio.run(seed())
        route_engine = create_async_engine(test_url, poolclass=NullPool)
        route_factory = async_sessionmaker(
            route_engine, class_=AsyncSession, expire_on_commit=False
        )
        redis = MockRedis()
        callback_user = UserSession(
            user_id=str(user_id),
            keycloak_id=f"kc-{user_id}",
            email=f"picker-{user_id}@test.invalid",
            role=default_role_name,
            is_admin=False,
            is_owner=False,
            role_confirmed=False,
        )
        MockKeycloakAdmin.user_session = callback_user

        class CallbackSessionContext:
            def __init__(self):
                self.session = None

            async def __aenter__(self):
                self.session = route_factory()
                return await self.session.__aenter__()

            async def __aexit__(self, exc_type, exc, traceback):
                return await self.session.__aexit__(exc_type, exc, traceback)

        async def override_get_session():
            async with route_factory() as session:
                yield session

        app = FastAPI()
        app.state.redis = redis
        register_error_handlers(app)
        app.include_router(onboarding_router)
        app.dependency_overrides[get_session] = override_get_session
        monkeypatch.setattr(main_module, "async_session", CallbackSessionContext)
        monkeypatch.setattr(main_module, "KeycloakAuthService", MockKeycloakAdmin)
        app.add_api_route(
            "/auth/callback", main_module.auth_callback, methods=["GET"], name="auth_callback"
        )

        @app.get("/")
        async def dashboard(user: UserSession = Depends(require_auth)):
            return {"role": user.role}

        client = TestClient(app, follow_redirects=False)
        yield SimpleNamespace(
            client=client,
            test_url=test_url,
            user_id=user_id,
            selected_role_id=selected_role_id,
            selected_role_name=selected_role_name,
        )

        app.dependency_overrides.clear()
        asyncio.run(route_engine.dispose())
        command.downgrade(config, "base")


def test_login_callback_picker_choose_role_dashboard_and_database_write(role_picker_env):
    env = role_picker_env
    callback = env.client.get("/auth/callback?code=mock-code&state=mock-state")
    assert callback.status_code == 303
    assert callback.headers["location"] == "/onboarding/role"

    picker = env.client.get("/onboarding/role", headers={"Accept": "text/html"})
    assert picker.status_code == 200
    assert env.selected_role_name in picker.text

    chosen = env.client.post(
        "/onboarding/role", data={"role_id": str(env.selected_role_id)}
    )
    assert chosen.status_code == 204
    assert chosen.headers["hx-redirect"] == "/"
    dashboard = env.client.get("/")
    assert dashboard.status_code == 200
    assert dashboard.json()["role"] == env.selected_role_name

    async def read_user() -> tuple[uuid.UUID, str]:
        engine = create_async_engine(env.test_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                user = (
                    await session.execute(select(User).where(User.id == env.user_id))
                ).scalar_one()
                return user.role_id, user.role_source
        finally:
            await engine.dispose()

    role_id, role_source = asyncio.run(read_user())
    assert role_id == env.selected_role_id
    assert role_source == "self_selected"


def test_decided_user_is_redirected_away_from_picker(role_picker_env):
    response = role_picker_env.client.get(
        "/onboarding/role", headers={"Accept": "text/html"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"