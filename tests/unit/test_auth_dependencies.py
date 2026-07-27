"""Tests for app/api/dependencies.py — require_auth / require_admin / require_owner."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import Request

from app.api.dependencies import require_admin, require_auth, require_owner
from app.core.exceptions import AuthenticationError, AuthorizationError
from tests.conftest import MockRedis, make_user_session, serialize_user_session


def _make_request(redis: MockRedis, session_id: str | None = None) -> Request:
    """Build a minimal Starlette Request with a Redis-backed app and optional cookie."""
    headers = []
    if session_id is not None:
        headers.append((b"cookie", f"session_id={session_id}".encode()))

    app = MagicMock()
    app.state.redis = redis

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "app": app,
    }
    return Request(scope)


class TestRequireAuth:

    async def test_require_auth_valid_session(self) -> None:
        session = make_user_session(email="yacin@whitecape.fr")
        redis = MockRedis({"session:abc123": serialize_user_session(session)})
        request = _make_request(redis, session_id="abc123")

        result = await require_auth(request)

        assert result.user_id == session.user_id
        assert result.email == "yacin@whitecape.fr"
        assert result.role == session.role
        assert result.is_admin == session.is_admin
        assert result.is_owner == session.is_owner

    async def test_require_auth_no_cookie(self) -> None:
        redis = MockRedis()
        request = _make_request(redis)

        with pytest.raises(AuthenticationError):
            await require_auth(request)

    async def test_require_auth_expired_session(self) -> None:
        redis = MockRedis()  # empty store — key missing
        request = _make_request(redis, session_id="abc123")

        with pytest.raises(AuthenticationError):
            await require_auth(request)


class TestRequireAdmin:

    async def test_require_admin_allows_admin_user(self) -> None:
        session = make_user_session(is_admin=True)

        result = await require_admin(session)

        assert result is session

    async def test_require_admin_rejects_non_admin(self) -> None:
        session = make_user_session(is_admin=False)

        with pytest.raises(AuthorizationError):
            await require_admin(session)


class TestRequireOwner:

    async def test_require_owner_allows_owner(self) -> None:
        session = make_user_session(is_admin=True, is_owner=True)

        result = await require_owner(session)

        assert result is session

    async def test_require_owner_rejects_admin_without_owner(self) -> None:
        session = make_user_session(is_admin=True, is_owner=False)

        with pytest.raises(AuthorizationError):
            await require_owner(session)
