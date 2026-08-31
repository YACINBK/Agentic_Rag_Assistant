"""The first-login role gate — an unconfirmed user can go nowhere but the picker.

M9c made the picker one-time, but nothing stopped navigation AROUND it: the nav
renders Documents/Users for admin/owner sessions regardless of confirmation, and
every route's own guards (require_auth / require_admin) pass — the session exists
and carries the flags. An admin who never picked a role could walk straight into
/admin/documents and operate with the born-default role (found in manual testing,
2026-08-31).

This middleware closes the loop: while the session says role_confirmed=False,
every path except the picker itself, the auth routes (login/logout/callback —
logout is the escape hatch), static assets, health and the dev harness redirects
to /onboarding/role. Three response shapes, mirroring the auth error handler:
HTMX gets an HX-Redirect header (full-page navigation), browsers get a 303,
API clients get JSON.

The gate is deliberately session-based, not DB-based: it must be cheap (every
request) and the session is what the routes themselves trust. The one split
case — session unconfirmed but the DB says decided (the user picked in another
tab, or an admin assigned the role) — is healed by the picker's GET, which
re-reads the DB and refreshes the session before redirecting; that is what
prevents a redirect loop here.
"""

from __future__ import annotations

import json

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.api.dependencies import SESSION_COOKIE
from app.core.security import UserSession

PICKER_PATH = "/onboarding/role"

# Reachable while unconfirmed: the picker itself, the auth routes (logout is the
# escape hatch — a user must always be able to leave), static assets, health,
# and the dev harness.
_EXEMPT_PREFIXES = ("/onboarding", "/auth", "/static", "/dev")


class RoleGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path == "/health" or path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return await call_next(request)  # anonymous — each route decides

        data = await request.app.state.redis.get(f"session:{session_id}")
        if not data:
            return await call_next(request)  # dead cookie — route auth will say so

        try:
            user = UserSession(**json.loads(data))
        except Exception:
            # Malformed payload: do not gate on it — the route's own session
            # validation is the authority on broken sessions.
            return await call_next(request)

        if user.role_confirmed:
            return await call_next(request)

        # First login, role not picked yet: the picker is the only way forward.
        if request.headers.get("hx-request", "").lower() == "true":
            return JSONResponse(
                status_code=409,
                content={"detail": "Select a role to continue"},
                headers={"HX-Redirect": PICKER_PATH},
            )
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url=PICKER_PATH, status_code=303)
        return JSONResponse(
            status_code=409, content={"detail": "Select a role to continue"}
        )
