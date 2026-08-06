from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette_csrf import CSRFMiddleware

from app.core.logging import configure_logging, get_logger
from app.core.models.base import async_session, engine
from app.core.settings import settings
from app.api.error_handlers import register_error_handlers
from app.api.routes import search_router
from app.services import qdrant_bootstrap
from app.services.auth import KeycloakAuthService, SESSION_COOKIE

configure_logging()
logger = get_logger(__name__)

templates = Jinja2Templates(directory="app/templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bootstrap Qdrant first and let it fail loudly: a running app with no
    # collections answers every query with "insufficient information" (contract M2).
    # Module-qualified call so tests can patch qdrant_bootstrap.ensure_collections.
    await qdrant_bootstrap.ensure_collections()
    app.state.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    logger.info("application_started")
    yield
    await app.state.redis.aclose()
    await engine.dispose()


app = FastAPI(
    title="Whitecape Knowledge Assistant",
    version="0.1.0",
    lifespan=lifespan,
)

register_error_handlers(app)

if not settings.DEV_MODE:
    app.add_middleware(
        CSRFMiddleware,
        secret=settings.SECRET_KEY,
        sensitive_cookies={SESSION_COOKIE},
        exempt_urls=["/auth/callback"],
    )
else:
    logger.warning(
        "dev_mode_enabled", hint="CSRF disabled, /dev routes active — DO NOT use in production"
    )

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(search_router)

if settings.DEV_MODE:
    # Imported inside the branch, not at module level: app/api/routes/dev.py is
    # local-only scaffolding and is not committed. A top-level import would make
    # startup fail with ImportError on any checkout that lacks it, DEV_MODE or not.
    from app.api.routes.dev import dev_router

    app.include_router(dev_router)


# --- Health ---


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- Auth routes ---


@app.get("/auth/login", name="login_page")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "pages/login.html")


@app.get("/auth/start")
async def auth_start(request: Request):
    async with async_session() as db:
        auth_service = KeycloakAuthService(db=db, redis=request.app.state.redis)
        url = await auth_service.get_authorization_url(request)
    return RedirectResponse(url)


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    async with async_session() as db:
        auth_service = KeycloakAuthService(db=db, redis=request.app.state.redis)
        try:
            await auth_service.handle_callback(request)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    session_id = request.state.session_id
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=False,  # Set True when behind TLS
        max_age=settings.SESSION_TTL_SECONDS,
    )
    return response


@app.get("/auth/logout")
async def auth_logout(request: Request):
    async with async_session() as db:
        auth_service = KeycloakAuthService(db=db, redis=request.app.state.redis)
        logout_url = await auth_service.logout(request)

    response = RedirectResponse(url=logout_url)
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- Pages ---


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # In dev mode, session lookup is Redis-only (no DB needed)
    user = None
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        import json

        data = await request.app.state.redis.get(f"session:{session_id}")
        if data:
            from app.core.security import UserSession

            user = UserSession(**json.loads(data))

    if not user and not settings.DEV_MODE:
        # Production: try Keycloak auth service
        async with async_session() as db:
            auth_service = KeycloakAuthService(db=db, redis=request.app.state.redis)
            user = await auth_service.get_current_user(request)

    if not user:
        if settings.DEV_MODE:
            return RedirectResponse(url="/dev/login")
        return RedirectResponse(url="/auth/login")

    return templates.TemplateResponse(request, "pages/dashboard.html", {"user": user})
