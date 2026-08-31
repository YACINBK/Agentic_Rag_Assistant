from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_auth
from app.core.models.base import get_session
from app.core.models.role import Role
from app.core.models.user import ROLE_SOURCE_DEFAULT, ROLE_SOURCE_SELF_SELECTED, User
from app.core.security import UserSession
from app.core.settings import settings

onboarding_router = APIRouter(prefix="/onboarding", tags=["onboarding"])
templates = Jinja2Templates(directory="app/templates")


@onboarding_router.get("/role")
async def role_picker(
    request: Request,
    user: UserSession = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Response:
    user_id = uuid.UUID(user.user_id)
    result = await session.execute(select(User.role_source).where(User.id == user_id))
    role_source = result.scalar_one()
    if role_source != ROLE_SOURCE_DEFAULT:
        return RedirectResponse(url="/", status_code=303)

    roles_result = await session.execute(select(Role).order_by(Role.name.asc()))
    roles = roles_result.scalars().all()
    return templates.TemplateResponse(
        request,
        "pages/onboarding_role.html",
        {"user": user, "roles": roles},
    )


@onboarding_router.post("/role")
async def choose_role(
    request: Request,
    role_id: uuid.UUID = Form(...),
    user: UserSession = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Response:
    user_id = uuid.UUID(user.user_id)
    user_result = await session.execute(select(User).where(User.id == user_id))
    db_user = user_result.scalar_one()
    if db_user.role_source != ROLE_SOURCE_DEFAULT:
        raise HTTPException(status_code=409, detail="Role already decided")

    role_result = await session.execute(select(Role).where(Role.id == role_id))
    role = role_result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=400, detail="Unknown role")

    db_user.role_id = role.id
    db_user.role_source = ROLE_SOURCE_SELF_SELECTED
    await session.commit()

    session_id = request.cookies["session_id"]
    session_key = f"session:{session_id}"
    session_data = await request.app.state.redis.get(session_key)
    session_payload = json.loads(session_data)
    session_payload["role"] = role.name
    session_payload["role_confirmed"] = True
    await request.app.state.redis.setex(
        session_key,
        settings.SESSION_TTL_SECONDS,
        json.dumps(session_payload),
    )

    return Response(status_code=204, headers={"HX-Redirect": "/"})