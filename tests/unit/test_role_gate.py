"""Tests for the first-login role gate (app/core/role_gate.py).

Found in manual testing 2026-08-31: an admin/owner who never picked a role
could click Documents/Users in the nav and operate the app on the born-default
role — every route's own guards passed because the session exists and carries
the flags. The gate makes the picker the only way forward while the session
says role_confirmed=False.

The mini-app mirrors the real wiring: the middleware plus one protected route,
MockRedis holding the session. follow_redirects=False everywhere — the gate's
job is to REDIRECT, and a following client would land on a picker route the
mini-app deliberately does not define (the picker's own behaviour is the M9c
suite's subject). The loop-breaker (picker GET heals a session whose DB row is
already decided) lives in onboarding.py and is covered by its own suite.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.role_gate import PICKER_PATH, RoleGateMiddleware
from tests.conftest import MockRedis, make_user_session, serialize_user_session

COOKIE = {"session_id": "gate-test"}
HTML = {"accept": "text/html"}
HTMX = {"hx-request": "true", "accept": "text/html"}
JSON = {"accept": "application/json"}


def _make_app(redis: MockRedis) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    app.add_middleware(RoleGateMiddleware)

    @app.get("/search")
    async def search():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


def _client(session) -> TestClient:
    redis = MockRedis({"session:gate-test": serialize_user_session(session)})
    return TestClient(_make_app(redis), follow_redirects=False)


class TestUnconfirmedUserIsGated:
    def test_browser_gets_303_to_picker(self) -> None:
        response = _client(make_user_session(role_confirmed=False)).get(
            "/search", headers=HTML, cookies=COOKIE
        )

        assert response.status_code == 303
        assert response.headers["location"] == PICKER_PATH

    def test_htmx_gets_hx_redirect(self) -> None:
        response = _client(make_user_session(role_confirmed=False)).get(
            "/search", headers=HTMX, cookies=COOKIE
        )

        assert response.headers["hx-redirect"] == PICKER_PATH

    def test_admin_flags_do_not_bypass_the_gate(self) -> None:
        """The exact leak from manual testing: is_admin/is_owner on the session
        must not buy passage — the picker is the only way forward."""
        response = _client(
            make_user_session(role_confirmed=False, is_admin=True, is_owner=True)
        ).get("/search", headers=HTML, cookies=COOKIE)

        assert response.status_code == 303
        assert response.headers["location"] == PICKER_PATH

    def test_api_client_gets_json_409(self) -> None:
        response = _client(make_user_session(role_confirmed=False)).get(
            "/search", headers=JSON, cookies=COOKIE
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "Select a role to continue"


class TestEveryoneElsePasses:
    def test_confirmed_session_passes(self) -> None:
        response = _client(make_user_session()).get("/search", cookies=COOKIE)

        assert response.status_code == 200

    def test_anonymous_passes_through(self) -> None:
        """No cookie — the gate stays silent; each route's own auth decides."""
        response = TestClient(_make_app(MockRedis()), follow_redirects=False).get(
            "/search", headers=HTML
        )

        assert response.status_code == 200  # the mini route has no auth of its own

    def test_dead_cookie_passes_through(self) -> None:
        client = TestClient(
            _make_app(MockRedis()), follow_redirects=False
        )

        response = client.get("/search", headers=HTML, cookies=COOKIE)

        assert response.status_code == 200

    def test_malformed_session_passes_through(self) -> None:
        """A broken payload is the route's problem, not the gate's — fail-open
        here is safe because every protected route re-validates the session."""
        client = TestClient(
            _make_app(MockRedis({"session:gate-test": "not-json"})),
            follow_redirects=False,
        )

        response = client.get("/search", headers=HTML, cookies=COOKIE)

        assert response.status_code == 200


class TestExemptPaths:
    def test_health_reachable_while_unconfirmed(self) -> None:
        response = _client(make_user_session(role_confirmed=False)).get(
            "/health", cookies=COOKIE
        )

        assert response.status_code == 200

    def test_picker_and_auth_reachable_while_unconfirmed(self) -> None:
        """The picker itself, and logout — the escape hatch — must never be
        gated, or an unconfirmed user could not even leave."""
        app = _make_app(MockRedis())

        @app.get(PICKER_PATH)
        async def picker():
            return {"ok": True}

        @app.get("/auth/logout")
        async def logout():
            return {"ok": True}

        client = TestClient(app, follow_redirects=False)

        assert (
            client.get(PICKER_PATH, headers=HTML, cookies=COOKIE).status_code == 200
        )
        assert (
            client.get("/auth/logout", headers=HTML, cookies=COOKIE).status_code == 200
        )
