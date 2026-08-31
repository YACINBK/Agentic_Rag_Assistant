from __future__ import annotations

import json
import secrets
from typing import Any

import httpx
import redis.asyncio as aioredis
from authlib.common.security import generate_token
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.core.models.role import Role
from app.core.models.user import ROLE_SOURCE_DEFAULT, User
from app.core.security import BaseAuthService, UserSession
from app.core.settings import settings
from app.services.sessions import register_session, unregister_session

logger = get_logger(__name__)

SESSION_COOKIE = "session_id"
SESSION_PREFIX = "session:"


class KeycloakAuthService(BaseAuthService):

    def __init__(self, db: AsyncSession, redis: aioredis.Redis) -> None:
        self._db = db
        self._redis = redis
        self._issuer = (
            f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}"
        )
        self._public_issuer = (
            f"{settings.KEYCLOAK_PUBLIC_URL}/realms/{settings.KEYCLOAK_REALM}"
        )
        self._auth_endpoint = f"{self._public_issuer}/protocol/openid-connect/auth"
        self._token_endpoint = f"{self._issuer}/protocol/openid-connect/token"
        self._userinfo_endpoint = f"{self._issuer}/protocol/openid-connect/userinfo"
        self._logout_endpoint = f"{self._public_issuer}/protocol/openid-connect/logout"

    def _build_oauth_client(self, redirect_uri: str) -> AsyncOAuth2Client:
        return AsyncOAuth2Client(
            client_id=settings.KEYCLOAK_CLIENT_ID,
            client_secret=settings.KEYCLOAK_CLIENT_SECRET,
            redirect_uri=redirect_uri,
            scope="openid profile email",
            code_challenge_method="S256",
        )

    async def get_authorization_url(self, request: Request) -> str:
        redirect_uri = str(request.url_for("auth_callback"))
        client = self._build_oauth_client(redirect_uri)

        # PKCE: the verifier is caller-supplied. authlib never generates or
        # stores it on the client, so we generate it here and stash it in Redis
        # against the state, to be presented at the token exchange.
        code_verifier = generate_token(48)
        uri, state = client.create_authorization_url(
            self._auth_endpoint, code_verifier=code_verifier
        )

        state_data = {
            "state": state,
            "code_verifier": code_verifier,
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

        # Exchange code for tokens, presenting the PKCE verifier stored at
        # /auth/start time.
        try:
            token = await client.fetch_token(
                self._token_endpoint,
                code=code,
                state=state,
                code_verifier=state_data["code_verifier"],
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
        # Reverse index, so an is_admin revocation can find this session later
        # (M9 D22). Registered after the session key exists: a member pointing at
        # a missing key is a harmless no-op for the purge, the reverse is not.
        await register_session(
            self._redis,
            user_session.user_id,
            session_id,
            settings.SESSION_TTL_SECONDS,
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
            # Read before delete: unregister_session needs the user id, and only
            # the stored session carries it — the cookie is an opaque id alone.
            user = await self.get_current_user(request)
            await self._redis.delete(f"{SESSION_PREFIX}{session_id}")
            if user is not None:
                await unregister_session(self._redis, user.user_id, session_id)

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

    async def _resolve_default_role(self) -> Role:
        """The Role a first-time user is created with, named by operator config.

        Differs from the pre-M9b auto-create in the one way that matters: the name
        comes from `settings.DEFAULT_ROLE`, never from a token, so a login cannot
        steer which Role gets created. A seeded database (CLAUDE.md §10 seeds
        `developer` and `qa_engineer`) never reaches the create branch.
        """
        result = await self._db.execute(
            select(Role).where(Role.name == settings.DEFAULT_ROLE)
        )
        role = result.scalar_one_or_none()

        if role is None:
            logger.warning(
                "default_role_missing_created_on_demand",
                default_role=settings.DEFAULT_ROLE,
            )
            # A reserved name raises ValueError from the validator at
            # app/core/models/role.py:35. Deliberately not caught: a misconfigured
            # DEFAULT_ROLE=admin must break this login loudly rather than write a
            # row that silently corrupts the CLAUDE.md §5 retrieval filter.
            role = Role(name=settings.DEFAULT_ROLE)
            self._db.add(role)
            await self._db.flush()

        return role

    async def _lazy_sync_user(self, userinfo: dict[str, Any]) -> UserSession:
        """Map a Keycloak identity onto a PostgreSQL user. Reads identity only.

        No role information is read from `userinfo`. A new user is created with the
        Role named by settings.DEFAULT_ROLE; an existing user keeps whatever role
        the database says, forever.

        PostgreSQL is authoritative for the primary role (CLAUDE.md §2 MVP
        amendment, D40). `realm_access` and every other authorization claim are
        ignored — not an error to be present, simply without effect.
        """
        keycloak_id = userinfo["sub"]
        email = userinfo.get("email", "")

        # selectinload is mandatory, not an optimisation: `user.role.name` is read
        # below to build the session, and a lazy load on an async session raises
        # MissingGreenlet at attribute access instead of emitting SQL — the same
        # trap app/api/routes/admin.py:254-263 documents for the row template.
        #
        # Lookup falls back to email. Matching by keycloak_id alone silently
        # orphans every existing row the moment Keycloak re-issues a user's sub
        # (a realm re-import into a fresh volume regenerates them — observed
        # live 2026-08-31: the missed lookup took the create branch and died on
        # the email unique constraint, a 500 on login). Email is the stable
        # identifier this realm guarantees: duplicateEmailsAllowed=false and
        # accounts exist only through realm administration
        # (registrationAllowed=false), so a Keycloak identity claiming an email
        # IS that user. Both columns are unique, so the OR matches at most one
        # row of each kind; the pathological two-row split would raise
        # MultipleResultsFound — loud, per §12's fail-loud preference.
        result = await self._db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(or_(User.keycloak_id == keycloak_id, User.email == email))
        )
        user = result.scalar_one_or_none()

        if user is None:
            role = await self._resolve_default_role()
            user = User(
                keycloak_id=keycloak_id,
                email=email,
                role_id=role.id,
                # Explicit, not left to the column's server_default: after flush a
                # server-default column is unloaded, and reading None would make
                # `!= "default"` report the session as confirmed.
                role_source=ROLE_SOURCE_DEFAULT,
            )
            self._db.add(user)
            await self._db.flush()
            # The Role object is already in hand — no second query, and no lazy
            # load against a freshly flushed user.
            role_name = role.name
        else:
            # Identity only. `user.role_id` is deliberately NOT written here: that
            # write is what made an admin's role assignment revert at the target's
            # next login (D21). Email still syncs — Keycloak owns identity.
            user.email = email
            if user.keycloak_id != keycloak_id:
                # Relink: the realm re-issued this user's sub (re-import). The
                # row is the same person — the email matched — so the stored
                # identity is refreshed, not the role. Flags and role survive.
                logger.info(
                    "user_identity_relinked",
                    email=email,
                    old_keycloak_id=str(user.keycloak_id),
                    new_keycloak_id=keycloak_id,
                )
                user.keycloak_id = keycloak_id
            await self._db.flush()
            role_name = user.role.name

        return UserSession(
            user_id=str(user.id),
            keycloak_id=keycloak_id,
            email=email,
            # The database row, never the claim. app/api/routes/search.py:167 feeds
            # this into the pipeline as `user_role`, one of the two dimensions of
            # the §5 retrieval filter — sourcing it from userinfo would leave
            # retrieval scope following Keycloak even with role_id left alone.
            role=role_name,
            is_admin=user.is_admin,
            is_owner=user.is_owner,
            role_confirmed=user.role_source not in (None, ROLE_SOURCE_DEFAULT),
        )
