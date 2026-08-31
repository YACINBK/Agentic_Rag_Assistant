"""Integration tests: full auth flow end-to-end via FastAPI TestClient.

Chain under test: route → require_auth dependency → auth service → MockRedis →
error handler → HTTP response. Only external Keycloak HTTP is mocked.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.api.error_handlers import register_error_handlers
from app.core.security import UserSession
from app.core.settings import settings
from app.services.auth import KeycloakAuthService
from tests.conftest import MockRedis, make_user_session, serialize_user_session

templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user_row(**overrides) -> SimpleNamespace:
    defaults = {
        "id": uuid.uuid4(),
        "email": "test@whitecape.fr",
        "keycloak_id": "kc-123",
        "role_id": uuid.uuid4(),
        "is_admin": False,
        "is_owner": False,
        # M9b: the session's role is now read off the row via the eager-loaded
        # relationship, and role_source decides UserSession.role_confirmed.
        # A double without both stands in for a User that cannot exist.
        "role_source": "admin_assigned",
        "role": SimpleNamespace(name="developer"),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_role_row(**overrides) -> SimpleNamespace:
    defaults = {"id": uuid.uuid4(), "name": "developer"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_db(role=None, user=None) -> AsyncMock:
    """Stand-in async session for the two queries `_lazy_sync_user` can issue.

    ORDER, not entity: User is selected first, and Role only afterwards and only
    on the new-user path (`_resolve_default_role`). An existing user consumes the
    first element alone. This ordering was reversed before M9b, when the Role
    lookup came first — leaving it reversed silently returns the *role* row where
    the *user* is expected, which surfaces as `AttributeError: 'SimpleNamespace'
    object has no attribute 'role'` inside the service rather than as a clear
    fixture failure. Dispatching on the selected entity instead of on call order
    would make that class of drift impossible; see D41.
    """
    db = AsyncMock()
    role_result = MagicMock()
    role_result.scalar_one_or_none.return_value = role
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = user
    db.execute.side_effect = [user_result, role_result]
    return db


def _mock_oauth_client(
    url: str = "http://keycloak:8080/realms/whitecape/protocol/openid-connect/auth?client_id=test",
    state: str = "state-123",
) -> MagicMock:
    client = MagicMock()
    client.create_authorization_url.return_value = (url, state)
    client.session_state = {"code_verifier": "verifier-123"}
    client.fetch_token = AsyncMock(return_value={"access_token": "tok-123"})
    return client


def _mock_httpx_userinfo(userinfo: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = userinfo

    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=response)
    http_client.__aenter__ = AsyncMock(return_value=http_client)
    http_client.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=http_client)


def _make_app(redis: MockRedis, db: AsyncMock) -> FastAPI:
    """Minimal FastAPI app mirroring the real app's auth wiring."""
    app = FastAPI()
    app.state.redis = redis
    register_error_handlers(app)

    @app.get("/auth/login", name="login_page")
    async def login_page(request: Request):
        # Mirrors the real app since 2026-08-31: no page here, a bounce to the
        # public landing. The route must exist under this name — Keycloak's
        # registered post_logout_redirect_uri points at it.
        return RedirectResponse(url="/")

    @app.get("/auth/start")
    async def auth_start(request: Request):
        service = KeycloakAuthService(db=db, redis=request.app.state.redis)
        url = await service.get_authorization_url(request)
        return RedirectResponse(url)

    @app.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        service = KeycloakAuthService(db=db, redis=request.app.state.redis)
        await service.handle_callback(request)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            "session_id", request.state.session_id, httponly=True, samesite="lax"
        )
        return response

    @app.get("/auth/logout")
    async def auth_logout(request: Request):
        service = KeycloakAuthService(db=db, redis=request.app.state.redis)
        logout_url = await service.logout(request)
        response = RedirectResponse(url=logout_url)
        response.delete_cookie("session_id")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        # Mirrors the real app's `/` since 2026-08-31: the public landing for
        # anonymous visitors (no redirect), the dashboard when a session exists.
        session_id = request.cookies.get("session_id")
        data = (
            await request.app.state.redis.get(f"session:{session_id}")
            if session_id
            else None
        )
        if data:
            user = UserSession(**json.loads(data))
            return templates.TemplateResponse(
                request, "pages/dashboard.html", {"user": user}
            )
        return templates.TemplateResponse(request, "pages/landing.html")

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAuthFlow:

    def test_unauthenticated_root_renders_landing(self) -> None:
        app = _make_app(MockRedis(), _make_db())
        client = TestClient(app, follow_redirects=False)

        response = client.get("/", headers={"Accept": "text/html"})

        # The first thing a visitor sees is the app's landing with the Login
        # button — not a redirect to a login page.
        assert response.status_code == 200
        assert "Login" in response.text
        assert 'href="/auth/start"' in response.text

    def test_login_route_bounces_to_landing(self) -> None:
        app = _make_app(MockRedis(), _make_db())
        client = TestClient(app, follow_redirects=False)

        response = client.get("/auth/login")

        # No page here anymore: expired sessions and Keycloak's post-logout
        # redirect both land on /auth/login and bounce to the landing.
        assert response.status_code in (302, 303, 307)
        assert response.headers["location"] == "/"

    def test_auth_start_redirects_to_keycloak(self) -> None:
        app = _make_app(MockRedis(), _make_db())
        client = TestClient(app, follow_redirects=False)

        kc_url = (
            "http://keycloak:8080/realms/whitecape/protocol/openid-connect/auth"
            "?client_id=test&state=state-123"
        )
        with patch(
            "app.services.auth.AsyncOAuth2Client",
            return_value=_mock_oauth_client(url=kc_url),
        ):
            response = client.get("/auth/start")

        assert response.status_code in (302, 303, 307)
        location = response.headers["location"]
        assert "keycloak" in location
        assert "openid-connect/auth" in location

    def test_full_callback_flow_sets_cookie(self) -> None:
        user = _make_user_row()
        role = _make_role_row()
        db = _make_db(role=role, user=user)
        redis = MockRedis(
            {
                "oauth_state:state-123": json.dumps(
                    {"state": "state-123", "code_verifier": "verifier-123"}
                )
            }
        )
        app = _make_app(redis, db)
        client = TestClient(app, follow_redirects=False)

        userinfo = {
            "sub": user.keycloak_id,
            "email": user.email,
            "realm_access": {"roles": ["developer"]},
        }
        with (
            patch(
                "app.services.auth.AsyncOAuth2Client",
                return_value=_mock_oauth_client(),
            ),
            patch(
                "app.services.auth.httpx.AsyncClient",
                _mock_httpx_userinfo(userinfo),
            ),
        ):
            response = client.get("/auth/callback?code=authcode123&state=state-123")

        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert "session_id=" in response.headers["set-cookie"]
        assert any(k.startswith("session:") for k in redis._store)

    def test_authenticated_root_shows_dashboard(self) -> None:
        session = make_user_session(email="yacin@whitecape.fr")
        redis = MockRedis({"session:abc123": serialize_user_session(session)})
        app = _make_app(redis, _make_db())
        client = TestClient(app)
        client.cookies.set("session_id", "abc123")

        response = client.get("/", headers={"Accept": "text/html"})

        assert response.status_code == 200
        assert "Welcome" in response.text or "yacin@whitecape.fr" in response.text

    def test_logout_clears_session(self) -> None:
        session = make_user_session()
        redis = MockRedis({"session:abc123": serialize_user_session(session)})
        app = _make_app(redis, _make_db())
        client = TestClient(app, follow_redirects=False)
        client.cookies.set("session_id", "abc123")

        # The logout redirect is browser-facing, so it is built from
        # KEYCLOAK_PUBLIC_URL (issuer split, daee865) — patching KEYCLOAK_URL
        # alone leaves the public URL in the Location header.
        with (
            patch.object(settings, "KEYCLOAK_URL", "http://keycloak:8080"),
            patch.object(settings, "KEYCLOAK_PUBLIC_URL", "http://keycloak:8080"),
        ):
            response = client.get("/auth/logout")

        assert response.status_code in (302, 303, 307)
        location = response.headers["location"]
        assert "keycloak" in location
        assert "logout" in location

        set_cookie = response.headers["set-cookie"]
        assert "session_id" in set_cookie
        assert 'session_id=""' in set_cookie or "Max-Age=0" in set_cookie

        assert "session:abc123" not in redis._store
