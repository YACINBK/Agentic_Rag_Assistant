from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette_csrf import CSRFMiddleware

from app.api.dependencies import SESSION_COOKIE, require_auth
from app.api.error_handlers import register_error_handlers
from app.api.routes.onboarding import onboarding_router
from app.core.models.base import get_session
from app.core.models.user import ROLE_SOURCE_ADMIN_ASSIGNED, ROLE_SOURCE_SELF_SELECTED
from app.core.settings import settings
from tests.conftest import MockRedis, make_role_model, make_user_model, make_user_session


class FakeResult:
    def __init__(self, value=None, rows=None):
        self.value = value
        self.rows = rows or []

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return SimpleNamespace(all=lambda: self.rows)


class FakeSession:
    def __init__(self, results):
        self.results = iter(results)
        self.commit = AsyncMock()

    async def execute(self, statement):
        return next(self.results)


def _make_client(redis, user, db_session) -> TestClient:
    app = FastAPI()
    app.state.redis = redis
    register_error_handlers(app)
    app.include_router(onboarding_router)
    app.dependency_overrides[require_auth] = lambda: user
    app.dependency_overrides[get_session] = lambda: db_session
    return TestClient(app, follow_redirects=False)


def test_picker_lists_every_role_from_the_table() -> None:
    roles = [
        make_role_model(id=uuid.uuid4(), name=f"scope-{index}")
        for index in (3, 1, 2)
    ]
    ordered_roles = sorted(roles, key=lambda role: role.name)
    db_user = make_user_model(id=uuid.uuid4())
    db_session = FakeSession([FakeResult(value="default"), FakeResult(rows=ordered_roles)])
    redis = MockRedis()
    user = make_user_session(user_id=str(db_user.id), role_confirmed=False)
    client = _make_client(redis, user, db_session)

    response = client.get("/onboarding/role")

    assert response.status_code == 200
    assert [role.name for role in ordered_roles] == sorted(
        role.name for role in ordered_roles
    )
    for role in ordered_roles:
        assert role.name in response.text
        assert f'value="{role.id}"' in response.text


def test_decided_user_is_bounced_off_the_picker() -> None:
    role = make_role_model(id=uuid.uuid4(), name=f"scope-{uuid.uuid4().hex}")
    db_user = make_user_model(id=uuid.uuid4(), role_source=ROLE_SOURCE_ADMIN_ASSIGNED)
    db_session = FakeSession([FakeResult(value=db_user.role_source)])
    client = _make_client(MockRedis(), make_user_session(user_id=str(db_user.id)), db_session)

    response = client.get("/onboarding/role")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert role.name not in response.text


def test_unknown_role_id_is_refused_without_a_write() -> None:
    db_user = make_user_model(id=uuid.uuid4())
    db_session = FakeSession([FakeResult(value=db_user), FakeResult(value=None)])
    client = _make_client(MockRedis(), make_user_session(user_id=str(db_user.id)), db_session)

    response = client.post("/onboarding/role", data={"role_id": str(uuid.uuid4())})

    assert response.status_code == 400
    assert db_user.role_source == "default"
    db_session.commit.assert_not_awaited()


def test_security_decided_user_cannot_pick_again() -> None:
    selected_role = make_role_model(id=uuid.uuid4(), name=f"scope-{uuid.uuid4().hex}")
    db_user = make_user_model(
        id=uuid.uuid4(), role_source=ROLE_SOURCE_SELF_SELECTED, role_id=uuid.uuid4()
    )
    original_role_id = db_user.role_id
    db_session = FakeSession([FakeResult(value=db_user)])
    client = _make_client(MockRedis(), make_user_session(user_id=str(db_user.id)), db_session)

    response = client.post("/onboarding/role", data={"role_id": str(selected_role.id)})

    assert response.status_code == 409
    assert db_user.role_id == original_role_id
    db_session.commit.assert_not_awaited()


def test_accepted_pick_writes_role_and_refreshes_the_same_session() -> None:
    chosen_role = make_role_model(id=uuid.uuid4(), name=f"scope-{uuid.uuid4().hex}")
    db_user = make_user_model(id=uuid.uuid4())
    session_id = f"session-{uuid.uuid4().hex}"
    user_session = make_user_session(user_id=str(db_user.id), role_confirmed=False)
    redis = MockRedis({f"session:{session_id}": json.dumps(user_session.__dict__)})
    db_session = FakeSession([FakeResult(value=db_user), FakeResult(value=chosen_role)])
    client = _make_client(redis, user_session, db_session)
    client.cookies.set("session_id", session_id)

    response = client.post("/onboarding/role", data={"role_id": str(chosen_role.id)})

    assert response.status_code == 204
    assert response.headers["hx-redirect"] == "/"
    assert db_user.role_id == chosen_role.id
    assert db_user.role_source == "self_selected"
    stored = json.loads(redis._store[f"session:{session_id}"])
    assert stored["role"] == chosen_role.name
    assert stored["role_confirmed"] is True
    assert f"session:{session_id}" in redis._store


def test_anonymous_visitor_never_sees_the_picker() -> None:
    app = FastAPI()
    app.state.redis = MockRedis()
    register_error_handlers(app)
    app.include_router(onboarding_router)
    client = TestClient(app, follow_redirects=False)

    response = client.get("/onboarding/role", headers={"Accept": "text/html"})

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"


def test_csrf_htmx_header_succeeds_and_bare_post_is_rejected() -> None:
    chosen_role = make_role_model(id=uuid.uuid4(), name=f"scope-{uuid.uuid4().hex}")
    db_user = make_user_model(id=uuid.uuid4())
    user_session = make_user_session(user_id=str(db_user.id), role_confirmed=False)
    redis = MockRedis()
    session_id = f"csrf-session-{uuid.uuid4().hex}"
    redis._store[f"session:{session_id}"] = json.dumps(user_session.__dict__)

    app = FastAPI()
    app.state.redis = redis
    register_error_handlers(app)
    app.add_middleware(
        CSRFMiddleware,
        secret=settings.SECRET_KEY,
        sensitive_cookies={SESSION_COOKIE},
        exempt_urls=[re.compile(r"^/auth/callback$")],
    )
    app.include_router(onboarding_router)
    app.dependency_overrides[require_auth] = lambda: user_session
    app.dependency_overrides[get_session] = lambda: FakeSession(
        [FakeResult(value=db_user), FakeResult(value=chosen_role)]
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, session_id)
    assert client.get("/health").status_code == 200
    csrf_token = client.cookies.get("csrftoken")
    assert csrf_token is not None

    without_header = client.post(
        "/onboarding/role", data={"role_id": str(chosen_role.id)}
    )
    assert without_header.status_code == 403

    with_header = client.post(
        "/onboarding/role",
        data={"role_id": str(chosen_role.id)},
        headers={"x-csrftoken": csrf_token},
    )
    assert with_header.status_code == 204


def test_callback_routes_undecided_users_to_the_picker(monkeypatch) -> None:
    import app.main as main_module

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class CallbackService:
        def __init__(self, db, redis):
            pass

        async def handle_callback(self, request):
            request.state.session_id = "callback-session"
            return callback_user

    monkeypatch.setattr(main_module, "async_session", lambda: SessionContext())
    monkeypatch.setattr(main_module, "KeycloakAuthService", CallbackService)
    main_module.app.state.redis = MockRedis()
    client = TestClient(main_module.app, follow_redirects=False)

    for confirmed, target in ((False, "/onboarding/role"), (True, "/")):
        callback_user = make_user_session(role_confirmed=confirmed)
        response = client.get("/auth/callback?code=code&state=state")
        assert response.status_code == 303
        assert response.headers["location"] == target