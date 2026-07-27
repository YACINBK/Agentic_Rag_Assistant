"""FastAPI auth dependencies — session validation and role-based authorization.

Dependency chain: require_owner → require_admin → require_auth.
Use with Depends() on any route that requires an authenticated or privileged user.

Session validation is Redis-only: no database, no Keycloak calls.
"""

from __future__ import annotations

import json

from fastapi import Depends, Request

from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.security import UserSession

SESSION_COOKIE = "session_id"
SESSION_PREFIX = "session:"


async def require_auth(request: Request) -> UserSession:
    """Read session cookie → Redis lookup → UserSession.

    Raises AuthenticationError if no valid session exists.
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise AuthenticationError("No session cookie present")

    data = await request.app.state.redis.get(f"{SESSION_PREFIX}{session_id}")
    if not data:
        raise AuthenticationError("Session expired or invalid")

    return UserSession(**json.loads(data))


async def require_admin(user: UserSession = Depends(require_auth)) -> UserSession:
    """Require is_admin flag on top of a valid session."""
    if not user.is_admin:
        raise AuthorizationError("Admin access required")
    return user


async def require_owner(user: UserSession = Depends(require_auth)) -> UserSession:
    """Require is_owner flag on top of a valid session."""
    if not user.is_owner:
        raise AuthorizationError("Owner access required")
    return user
