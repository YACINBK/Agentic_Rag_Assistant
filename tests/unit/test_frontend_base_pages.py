"""Tests for base pages: landing.html and dashboard.html (macro-driven).

The landing is the public entry at `/` — an unauthenticated visitor sees it
with the Login button (the OIDC entry point), not a redirect to a login page.
Dashboard is what `/` renders once a session exists. Both extend base.html and
use macros from Module 1. Amended 2026-08-31: the standalone login page was
replaced by the landing; `/auth/login` is now a redirect to `/`.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.core.security import UserSession
from tests.conftest import MockRedis, make_user_session, serialize_user_session

_templates = Jinja2Templates(directory="app/templates")


def _make_app(redis: MockRedis) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis

    # Mirrors the real app's `/`: public landing when no session, dashboard
    # when one exists (app/main.py does the same inline lookup).
    @app.get("/")
    async def index(request: Request):
        import json

        session_id = request.cookies.get("session_id")
        data = await redis.get(f"session:{session_id}") if session_id else None
        if data:
            user = UserSession(**json.loads(data))
            return _templates.TemplateResponse(
                request, "pages/dashboard.html", {"user": user}
            )
        return _templates.TemplateResponse(request, "pages/landing.html")

    return app


def _authenticated_client(session: UserSession) -> TestClient:
    redis = MockRedis({"session:abc123": serialize_user_session(session)})
    client = TestClient(_make_app(redis))
    client.cookies.set("session_id", "abc123")
    return client


class TestLandingPage:
    """The public entry — what an unauthenticated visitor sees at `/`."""

    def test_landing_renders_for_anonymous_visitor(self) -> None:
        client = TestClient(_make_app(MockRedis()))

        response = client.get("/")

        assert response.status_code == 200
        assert "Login" in response.text
        assert "Whitecape Knowledge" in response.text  # extends base.html

    def test_landing_login_button_targets_auth_start(self) -> None:
        client = TestClient(_make_app(MockRedis()))

        response = client.get("/")

        # The Login button IS the OIDC entry point — no separate login page.
        assert 'href="/auth/start"' in response.text
        # The button says Login, not "Sign in with Keycloak": Keycloak is the
        # mechanism, not something the user should be asked to care about.
        assert "Keycloak" not in response.text

    def test_landing_no_user_nav(self) -> None:
        client = TestClient(_make_app(MockRedis()))

        response = client.get("/")

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
