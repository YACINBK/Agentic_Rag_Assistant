"""Tests for base pages: login.html and dashboard.html (macro-driven).

Login page is public. Dashboard requires auth — MockRedis provides the session.
Both extend base.html and use macros from Module 1.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.api.dependencies import require_auth
from app.core.security import UserSession
from tests.conftest import MockRedis, make_user_session, serialize_user_session

_templates = Jinja2Templates(directory="app/templates")


def _make_app(redis: MockRedis) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis

    @app.get("/auth/login", name="login_page")
    async def login_page(request: Request):
        return _templates.TemplateResponse(request, "pages/login.html")

    @app.get("/")
    async def index(request: Request, user: UserSession = Depends(require_auth)):
        return _templates.TemplateResponse(request, "pages/dashboard.html", {"user": user})

    return app


def _authenticated_client(session: UserSession) -> TestClient:
    redis = MockRedis({"session:abc123": serialize_user_session(session)})
    client = TestClient(_make_app(redis))
    client.cookies.set("session_id", "abc123")
    return client


class TestLoginPage:

    def test_login_page_renders(self) -> None:
        client = TestClient(_make_app(MockRedis()))

        response = client.get("/auth/login")

        assert response.status_code == 200
        assert "Sign in" in response.text
        assert "Whitecape Knowledge" in response.text  # extends base.html

    def test_login_page_has_keycloak_link(self) -> None:
        client = TestClient(_make_app(MockRedis()))

        response = client.get("/auth/login")

        assert 'href="/auth/start"' in response.text

    def test_login_page_no_user_nav(self) -> None:
        client = TestClient(_make_app(MockRedis()))

        response = client.get("/auth/login")

        assert "Logout" not in response.text


class TestDashboardPage:

    def test_dashboard_shows_email(self) -> None:
        client = _authenticated_client(make_user_session(email="test@whitecape.com"))

        response = client.get("/")

        assert response.status_code == 200
        assert "test@whitecape.com" in response.text
        assert "Whitecape Knowledge" in response.text  # extends base.html

    def test_dashboard_shows_role(self) -> None:
        client = _authenticated_client(make_user_session(role="developer"))

        response = client.get("/")

        assert "developer" in response.text

    def test_dashboard_admin_badge(self) -> None:
        client = _authenticated_client(make_user_session(is_admin=True))

        response = client.get("/")

        assert "Admin" in response.text

    def test_dashboard_search_link(self) -> None:
        client = _authenticated_client(make_user_session())

        response = client.get("/")

        assert 'href="/search"' in response.text
