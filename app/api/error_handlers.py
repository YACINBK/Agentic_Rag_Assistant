"""FastAPI exception handlers for auth errors.

Converts AuthenticationError / AuthorizationError into client-appropriate
responses: browser redirects/pages, HTMX partials, or JSON for API clients.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.exceptions import AuthenticationError, AuthorizationError

templates = Jinja2Templates(directory="app/templates")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _accepts_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def register_error_handlers(app: FastAPI) -> None:
    """Register auth exception handlers. Call once at app startup."""

    @app.exception_handler(AuthenticationError)
    async def authentication_error_handler(
        request: Request, exc: AuthenticationError
    ):
        if _is_htmx(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"HX-Redirect": "/auth/login"},
            )
        if _accepts_html(request):
            return RedirectResponse(url="/auth/login", status_code=303)
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
        )

    @app.exception_handler(AuthorizationError)
    async def authorization_error_handler(
        request: Request, exc: AuthorizationError
    ):
        if _is_htmx(request):
            return templates.TemplateResponse(
                request, "partials/403.html", status_code=403
            )
        if _accepts_html(request):
            return templates.TemplateResponse(
                request, "pages/403.html", status_code=403
            )
        return JSONResponse(
            status_code=403,
            content={"detail": "Insufficient permissions"},
        )
