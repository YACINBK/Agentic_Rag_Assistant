"""Tests for app/api/error_handlers.py — auth exception → client-appropriate response."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.error_handlers import register_error_handlers
from app.core.exceptions import AuthenticationError, AuthorizationError


def _make_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise-auth")
    async def raise_auth():
        raise AuthenticationError("test auth failure")

    @app.get("/raise-authz")
    async def raise_authz():
        raise AuthorizationError("test authz failure")

    return app


class TestAuthenticationErrorHandling:

    def test_auth_error_redirects_browser(self) -> None:
        client = TestClient(_make_app(), follow_redirects=False)

        response = client.get("/raise-auth", headers={"Accept": "text/html"})

        assert response.status_code == 303
        assert response.headers["location"] == "/auth/login"

    def test_auth_error_htmx_redirect(self) -> None:
        client = TestClient(_make_app())

        response = client.get("/raise-auth", headers={"HX-Request": "true"})

        assert response.status_code == 401
        assert response.headers["hx-redirect"] == "/auth/login"

    def test_auth_error_json_response(self) -> None:
        client = TestClient(_make_app())

        response = client.get("/raise-auth", headers={"Accept": "application/json"})

        assert response.status_code == 401
        assert "detail" in response.json()


class TestAuthorizationErrorHandling:

    def test_authz_error_returns_403_page(self) -> None:
        client = TestClient(_make_app())

        response = client.get("/raise-authz", headers={"Accept": "text/html"})

        assert response.status_code == 403
        body = response.text.lower()
        assert "forbidden" in body or "permission" in body

    def test_authz_error_htmx_returns_partial(self) -> None:
        client = TestClient(_make_app())

        response = client.get("/raise-authz", headers={"HX-Request": "true"})

        assert response.status_code == 403
        assert "<html" not in response.text.lower()

    def test_authz_error_json_response(self) -> None:
        client = TestClient(_make_app())

        response = client.get("/raise-authz", headers={"Accept": "application/json"})

        assert response.status_code == 403
        assert response.json() == {"detail": "Insufficient permissions"}
