"""Owner seeding integration tests against the isolated Alembic database."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

import scripts.seed_owner as seed_owner_module
from app.core.models.role import Role
from app.core.models.user import User
from app.core.settings import settings
from tests.conftest import require_test_database_url

pytestmark = pytest.mark.integration


def _reset_schema(test_url: str) -> None:
    async def reset() -> None:
        engine = create_async_engine(test_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("DROP SCHEMA public CASCADE"))
                await connection.execute(text("CREATE SCHEMA public"))
        finally:
            await engine.dispose()

    asyncio.run(reset())


@pytest.fixture(scope="module")
def alembic_env():
    test_url = require_test_database_url("owner seeding")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(settings, "DATABASE_URL", test_url)
        config = Config("alembic.ini")
        _reset_schema(test_url)
        command.upgrade(config, "head")
        yield test_url
        command.downgrade(config, "base")


def _run_scenario(test_url: str, scenario):
    async def run():
        engine = create_async_engine(test_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            # Use the factory to provide the session to the script
            with patch.object(seed_owner_module, "async_session", factory):
                return await scenario(factory)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_seed_new_user(alembic_env):
    email = f"new-{uuid.uuid4()}@example.com"

    async def scenario(factory):
        async with factory() as session:
            await session.execute(delete(User))
            await session.commit()
        await seed_owner_module.seed_owner(email)
        async with factory() as session:
            user = (
                await session.execute(
                    select(User).options(selectinload(User.role)).where(User.email == email)
                )
            ).scalar_one()
            return user.is_owner, user.is_admin, user.role.name, user.role_source

    assert _run_scenario(alembic_env, scenario) == (True, True, settings.DEFAULT_ROLE, "default")


def test_seed_existing_user(alembic_env):
    email = f"existing-{uuid.uuid4()}@example.com"

    async def scenario(factory):
        async with factory() as session:
            await session.execute(delete(User))
            await session.commit()
            role = (
                await session.execute(select(Role).where(Role.name == settings.DEFAULT_ROLE))
            ).scalar_one()
            session.add(User(email=email, keycloak_id=f"kc-{uuid.uuid4()}", role_id=role.id))
            await session.commit()
        await seed_owner_module.seed_owner(email)
        async with factory() as session:
            user = (await session.execute(select(User).where(User.email == email))).scalar_one()
            return user.is_owner, user.is_admin

    assert _run_scenario(alembic_env, scenario) == (True, True)


def test_seed_idempotency(alembic_env):
    email = f"idempotent-{uuid.uuid4()}@example.com"

    async def scenario(factory):
        async with factory() as session:
            await session.execute(delete(User))
            await session.commit()
        await seed_owner_module.seed_owner(email)
        await seed_owner_module.seed_owner(email)
        async with factory() as session:
            result = await session.execute(select(User).where(User.email == email))
            return len(result.scalars().all())

    assert _run_scenario(alembic_env, scenario) == 1


def test_single_owner_constraint(alembic_env):
    first_email = f"owner-a-{uuid.uuid4()}@example.com"
    second_email = f"owner-b-{uuid.uuid4()}@example.com"

    async def scenario(factory):
        async with factory() as session:
            await session.execute(delete(User))
            await session.commit()
        await seed_owner_module.seed_owner(first_email)
        with pytest.raises(SystemExit) as exc_info:
            await seed_owner_module.seed_owner(second_email)
        async with factory() as session:
            owners = (
                await session.execute(select(User).where(User.is_owner.is_(True)))
            ).scalars().all()
            return exc_info.value.code, len(owners), owners[0].email

    assert _run_scenario(alembic_env, scenario) == (1, 1, first_email)
