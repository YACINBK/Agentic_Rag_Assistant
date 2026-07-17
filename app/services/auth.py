from __future__ import annotations

import json
import secrets
from typing import Any

import httpx
import redis.asyncio as aioredis
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError
from app.core.models.role import Role
from app.core.models.user import User
from app.core.security import BaseAuthService, UserSession
from app.core.settings import settings

SESSION_COOKIE = "session_id"
SESSION_PREFIX = "session:"


class KeycloakAuthService(BaseAuthService):

    def __init__(self, db: AsyncSession, redis: aioredis.Redis) -> None:
        self._db = db
        self._redis = redis
        self._issuer = (
            f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}"
        )
        self._auth_endpoint = f"{self._issuer}/protocol/openid-connect/auth"
        self._token_endpoint = f"{self._issuer}/protocol/openid-connect/token"
        self._userinfo_endpoint = f"{self._issuer}/protocol/openid-connect/userinfo"
        self._logout_endpoint = f"{self._issuer}/protocol/openid-connect/logout"

    def _build_oauth_client(self, redirect_uri: str) -> AsyncOAuth2Client:
        return AsyncOAuth2Client(
            client_id=settings.KEYCLOAK_CLIENT_ID,
            client_secret=settings.KEYCLOAK_CLIENT_SECRET,
            redirect_uri=redirect_uri,
            code_challenge_method="S256",
        )

    async def get_authorization_url(self, request: Request) -> str:
        redirect_uri = str(request.url_for("auth_callback"))
        client = self._build_oauth_client(redirect_uri)

        uri, state = client.create_authorization_url(self._auth_endpoint)

        # Store state + PKCE verifier in a short-lived Redis key
        state_data = {
            "state": state,
            "code_verifier": client.session_state.get("code_verifier", ""),
        }
        await self._redis.setex(
            f"oauth_state:{state}", 300, json.dumps(state_data)
        )
        return uri

    async def handle_callback(self, request: Request) -> UserSession:
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            raise AuthenticationError("Missing code or state in callback")

        # Retrieve stored state
        state_key = f"oauth_state:{state}"
        stored = await self._redis.get(state_key)
        if not stored:
            raise AuthenticationError("Invalid or expired OAuth state")
        await self._redis.delete(state_key)

        state_data = json.loads(stored)
        redirect_uri = str(request.url_for("auth_callback"))
        client = self._build_oauth_client(redirect_uri)
        client.session_state["code_verifier"] = state_data["code_verifier"]

        # Exchange code for tokens
        try:
            token = await client.fetch_token(
                self._token_endpoint,
                code=code,
                state=state,
            )
        except Exception as e:
            raise AuthenticationError(f"Token exchange failed: {e}") from e

        # Get user info from Keycloak
        userinfo = await self._fetch_userinfo(token["access_token"])

        # Lazy sync to database
        user_session = await self._lazy_sync_user(userinfo)

        # Create server-side session
        session_id = secrets.token_urlsafe(32)
        session_data = json.dumps(user_session.__dict__)
        await self._redis.setex(
            f"{SESSION_PREFIX}{session_id}",
            settings.SESSION_TTL_SECONDS,
            session_data,
        )

        # Attach session_id to request state so the route can set the cookie
        request.state.session_id = session_id
        return user_session

    async def get_current_user(self, request: Request) -> UserSession | None:
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return None

        data = await self._redis.get(f"{SESSION_PREFIX}{session_id}")
        if not data:
            return None

        payload = json.loads(data)
        return UserSession(**payload)

    async def logout(self, request: Request) -> str:
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            await self._redis.delete(f"{SESSION_PREFIX}{session_id}")

        return (
            f"{self._logout_endpoint}"
            f"?client_id={settings.KEYCLOAK_CLIENT_ID}"
            f"&post_logout_redirect_uri={request.url_for('login_page')}"
        )

    async def _fetch_userinfo(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                self._userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                raise AuthenticationError("Failed to fetch userinfo from Keycloak")
            return resp.json()

    async def _lazy_sync_user(self, userinfo: dict[str, Any]) -> UserSession:
        keycloak_id = userinfo["sub"]
        email = userinfo.get("email", "")

        # Extract role from realm_access (Keycloak convention)
        realm_roles = userinfo.get("realm_access", {}).get("roles", [])
        role_name = next(
            (r for r in realm_roles if r not in ("offline_access", "uma_authorization", "default-roles-whitecape")),
            "user",
        )

        # Find or create the role
        result = await self._db.execute(select(Role).where(Role.name == role_name))
        role = result.scalar_one_or_none()
        if role is None:
            role = Role(name=role_name)
            self._db.add(role)
            await self._db.flush()

        # Find or create the user
        result = await self._db.execute(
            select(User).where(User.keycloak_id == keycloak_id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                keycloak_id=keycloak_id,
                email=email,
                role_id=role.id,
            )
            self._db.add(user)
            await self._db.flush()
        else:
            user.email = email
            user.role_id = role.id
            await self._db.flush()

        await self._db.commit()

        return UserSession(
            user_id=str(user.id),
            keycloak_id=keycloak_id,
            email=email,
            role=role_name,
            is_admin=user.is_admin,
            is_owner=user.is_owner,
        )
