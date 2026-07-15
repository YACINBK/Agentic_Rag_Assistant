"""JWT authentication and authorization.

Keycloak JWT validation via JWKS public keys.
Lazy sync on login — no webhook infrastructure.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenClaims:
    """Extracted JWT claims after validation."""

    keycloak_id: str
    email: str
    role: str  # primary role name from Keycloak realm


class BaseAuthService(ABC):

    @abstractmethod
    async def validate_token(self, token: str) -> TokenClaims:
        """Validate JWT signature via JWKS and extract claims.

        Raises AuthenticationError if token is invalid or expired.
        """
        ...

    @abstractmethod
    async def lazy_sync_user(self, claims: TokenClaims) -> dict:
        """Upsert User record from JWT claims.

        Creates on first login, updates on subsequent logins.
        Returns the user dict (id, role, is_admin, is_owner).
        """
        ...
