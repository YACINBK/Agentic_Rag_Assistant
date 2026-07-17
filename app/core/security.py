"""Session-based OIDC authentication and authorization.

Keycloak OIDC via authlib. Server-side sessions stored in Redis.
HTTP-only cookie. Lazy user sync on login. No JWT validation in app.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True)
class UserSession:
    """User data stored in the server-side session after OIDC login."""

    user_id: str
    keycloak_id: str
    email: str
    role: str
    is_admin: bool
    is_owner: bool


class BaseAuthService(ABC):

    @abstractmethod
    async def get_authorization_url(self, request: Request) -> str:
        """Generate Keycloak OIDC authorization URL with PKCE.

        Returns the redirect URL the user should be sent to.
        """
        ...

    @abstractmethod
    async def handle_callback(self, request: Request) -> UserSession:
        """Exchange authorization code for tokens, sync user, create session.

        Called when Keycloak redirects back with ?code=... and ?state=...
        Returns UserSession to be stored in the session store.
        Raises AuthenticationError if code exchange or validation fails.
        """
        ...

    @abstractmethod
    async def get_current_user(self, request: Request) -> UserSession | None:
        """Read session from request cookie and return user data.

        Returns None if no valid session exists (unauthenticated).
        """
        ...

    @abstractmethod
    async def logout(self, request: Request) -> str:
        """Destroy session and return Keycloak logout URL.

        Returns the URL to redirect the user to for Keycloak-side logout.
        """
        ...
