"""Unit tests for KeycloakAuthService (app/services/auth.py).

Test-only contract — no production code modified.
Everything external is mocked: Keycloak HTTP, OAuth2 client, Redis, PostgreSQL.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import AuthenticationError
from app.core.settings import settings
from app.services.auth import KeycloakAuthService
from tests.conftest import (
    MockRedis,
    make_user_session,
    serialize_user_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user_row(**overrides) -> SimpleNamespace:
    """Stand-in for a User ORM row. The service only reads/writes plain attributes."""
    defaults = {
        "id": uuid.uuid4(),
        "email": "test@whitecape.fr",
        "keycloak_id": "kc-123",
        "role_id": uuid.uuid4(),
        "is_admin": False,
        "is_owner": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_role_row(**overrides) -> SimpleNamespace:
    """Stand-in for a Role ORM row."""
    defaults = {"id": uuid.uuid4(), "name": "developer"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    query_params: dict | None = None,
    cookies: dict | None = None,
) -> MagicMock:
    """Request mock: dict-like query_params/cookies, string url_for, assignable state."""
    request = MagicMock()
    request.query_params = query_params or {}
    request.cookies = cookies or {}
    request.url_for = lambda name: f"http://testserver/{name}"
    request.state = SimpleNamespace()
    return request


def _make_db(role=None, user=None) -> AsyncMock:
    """AsyncSession mock: two execute() calls — first Role, then User."""
    db = AsyncMock()
    role_result = MagicMock()
    role_result.scalar_one_or_none.return_value = role
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = user
    db.execute.side_effect = [role_result, user_result]
    return db


def _mock_oauth_client(
    url: str = "http://keycloak:8080/realms/whitecape/protocol/openid-connect/auth?client_id=test",
    state: str = "state-123",
    token: dict | None = None,
) -> MagicMock:
    """AsyncOAuth2Client mock: create_authorization_url → (url, state), fetch_token → token."""
    client = MagicMock()
    client.create_authorization_url.return_value = (url, state)
    client.session_state = {"code_verifier": "verifier-123"}
    client.fetch_token = AsyncMock(return_value=token or {"access_token": "tok-123"})
    return client


def _mock_httpx_userinfo(userinfo: dict, status_code: int = 200) -> MagicMock:
    """httpx.AsyncClient class mock supporting 'async with' and GET /userinfo."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = userinfo

    http_client = MagicMock()
    http_client.get = AsyncMock(return_value=response)
    http_client.__aenter__ = AsyncMock(return_value=http_client)
    http_client.__aexit__ = AsyncMock(return_value=None)

    return MagicMock(return_value=http_client)


# ---------------------------------------------------------------------------
# get_authorization_url
# ---------------------------------------------------------------------------


class TestGetAuthorizationUrl:

    @pytest.mark.asyncio
    async def test_authorization_url_contains_keycloak_endpoint(self) -> None:
        redis = MockRedis()
        db = _make_db()
        with (
            patch.object(settings, "KEYCLOAK_URL", "http://keycloak:8080"),
            patch.object(settings, "KEYCLOAK_REALM", "whitecape"),
        ):
            service = KeycloakAuthService(db=db, redis=redis)

        expected_url = (
            "http://keycloak:8080/realms/whitecape/protocol/openid-connect/auth"
            "?client_id=test&state=state-123"
        )
        with patch(
            "app.services.auth.AsyncOAuth2Client",
            return_value=_mock_oauth_client(url=expected_url),
        ):
            url = await service.get_authorization_url(_make_request())

        assert "/realms/whitecape/protocol/openid-connect/auth" in url

    @pytest.mark.asyncio
    async def test_authorization_url_stores_state_in_redis(self) -> None:
        redis = MockRedis()
        db = _make_db()
        service = KeycloakAuthService(db=db, redis=redis)

        with patch(
            "app.services.auth.AsyncOAuth2Client",
            return_value=_mock_oauth_client(state="state-123"),
        ):
            await service.get_authorization_url(_make_request())

        oauth_keys = [k for k in redis._store if k.startswith("oauth_state:")]
        assert len(oauth_keys) == 1
        assert oauth_keys[0] == "oauth_state:state-123"


# ---------------------------------------------------------------------------
# handle_callback
# ---------------------------------------------------------------------------


class TestHandleCallback:

    @pytest.mark.asyncio
    async def test_callback_creates_session(self) -> None:
        user = _make_user_row(is_admin=False, is_owner=False)
        role = _make_role_row()
        db = _make_db(role=role, user=user)
        redis = MockRedis(
            {
                "oauth_state:state-123": json.dumps(
                    {"state": "state-123", "code_verifier": "verifier-123"}
                )
            }
        )
        service = KeycloakAuthService(db=db, redis=redis)
        request = _make_request(query_params={"code": "code-abc", "state": "state-123"})

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
            session = await service.handle_callback(request)

        assert session.keycloak_id == user.keycloak_id
        assert session.email == user.email
        assert session.role == "developer"
        assert session.is_admin is False
        assert session.is_owner is False

        session_keys = [k for k in redis._store if k.startswith("session:")]
        assert len(session_keys) == 1

        # OAuth state key must be consumed (deleted) after use
        assert "oauth_state:state-123" not in redis._store

    @pytest.mark.asyncio
    async def test_callback_missing_code_raises(self) -> None:
        service = KeycloakAuthService(db=_make_db(), redis=MockRedis())
        request = _make_request(query_params={"state": "abc"})  # no code

        with pytest.raises(AuthenticationError, match="Missing code or state"):
            await service.handle_callback(request)

    @pytest.mark.asyncio
    async def test_callback_expired_state_raises(self) -> None:
        service = KeycloakAuthService(db=_make_db(), redis=MockRedis())  # empty store
        request = _make_request(query_params={"code": "code-abc", "state": "gone"})

        with pytest.raises(AuthenticationError, match="Invalid or expired OAuth state"):
            await service.handle_callback(request)


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------


class TestGetCurrentUser:

    @pytest.mark.asyncio
    async def test_get_current_user_valid_session(self) -> None:
        stored = make_user_session(email="stored@whitecape.fr")
        redis = MockRedis({"session:abc123": serialize_user_session(stored)})
        service = KeycloakAuthService(db=_make_db(), redis=redis)
        request = _make_request(cookies={"session_id": "abc123"})

        result = await service.get_current_user(request)

        assert result is not None
        assert result.user_id == stored.user_id
        assert result.email == "stored@whitecape.fr"
        assert result.role == stored.role

    @pytest.mark.asyncio
    async def test_get_current_user_no_session(self) -> None:
        service = KeycloakAuthService(db=_make_db(), redis=MockRedis())  # empty store
        request = _make_request(cookies={"session_id": "expired"})

        result = await service.get_current_user(request)

        assert result is None
