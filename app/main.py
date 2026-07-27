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
from app.services.auth import KeycloakAuthService, SESSION_COOKIE

configure_logging()
logger = get_logger(__name__)

templates = Jinja2Templates(directory="app/templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
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

app.add_middleware(
    CSRFMiddleware,
    secret=settings.SECRET_KEY,
    sensitive_cookies={SESSION_COOKIE},
    exempt_urls=["/auth/callback"],
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


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
    async with async_session() as db:
        auth_service = KeycloakAuthService(db=db, redis=request.app.state.redis)
        user = await auth_service.get_current_user(request)

    if not user:
        return RedirectResponse(url="/auth/login")

    return templates.TemplateResponse(request, "pages/dashboard.html", {"user": user})
