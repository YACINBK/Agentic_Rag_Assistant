from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError
from app.core.models.user import User
from app.core.security import BaseAuthService, TokenClaims
from app.core.settings import settings


class KeycloakAuthService(BaseAuthService):

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._jwks_uri = (
            f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}"
            "/protocol/openid-connect/certs"
        )
        self._jwks: dict | None = None

    async def _get_jwks(self) -> dict:
        if self._jwks is None:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self._jwks_uri)
                resp.raise_for_status()
                self._jwks = resp.json()
        return self._jwks

    async def validate_token(self, token: str) -> TokenClaims:
        import jwt as pyjwt
        from jwt import PyJWKClient

        jwks_data = await self._get_jwks()
        jwk_client = PyJWKClient("")
        jwk_client.fetch_data = lambda: jwks_data

        try:
            signing_key = pyjwt.PyJWKClient(self._jwks_uri).get_signing_key_from_jwt(token)
            payload = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=settings.KEYCLOAK_CLIENT_ID,
            )
        except pyjwt.exceptions.PyJWTError as e:
            raise AuthenticationError(f"Invalid token: {e}") from e

        realm_access = payload.get("realm_access", {})
        roles = realm_access.get("roles", [])
        role = next((r for r in roles if r not in ("offline_access", "uma_authorization")), "user")

        return TokenClaims(
            keycloak_id=payload["sub"],
            email=payload.get("email", ""),
            role=role,
        )

    async def lazy_sync_user(self, claims: TokenClaims) -> dict:
        result = await self._session.execute(
            select(User).where(User.keycloak_id == claims.keycloak_id)
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                keycloak_id=claims.keycloak_id,
                email=claims.email,
                role_name=claims.role,
            )
            self._session.add(user)
            await self._session.flush()
        else:
            user.email = claims.email
            user.role_name = claims.role
            await self._session.flush()

        return {
            "id": str(user.id),
            "role": user.role_name,
            "is_admin": user.is_admin,
            "is_owner": user.is_owner,
        }
