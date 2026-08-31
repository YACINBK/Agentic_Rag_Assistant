"""M9b — integration tests against real PostgreSQL.

Contract: contracts/m9b_role_authority.md, test cases 5–6 (assertions 9 and 10).
These two assertions are not provable against a mock: a mocked session never
raises `MissingGreenlet`, and a mocked migration never leaves an enum type behind.

Requires the runbook preamble — `set -a; . .env.local; set +a` with postgres up.
Without `TEST_DATABASE_URL` these skip, and a skip is not a pass.

All test functions are sync (not `async def`): alembic's upgrade/downgrade drive
their own event loop, and DB reads use `asyncio.run(...)`. Same shape as
tests/integration/test_m1_migration.py.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.models.user import ROLE_SOURCE_ADMIN_ASSIGNED, ROLE_SOURCE_DEFAULT, User
from app.core.settings import settings
from app.services.auth import KeycloakAuthService
from tests.conftest import (
    SEED_ROLE_DEVELOPER_ID,
    MockRedis,
    require_test_database_url,
)

pytestmark = pytest.mark.integration

# The revision immediately before M9b — the schema without `user.role_source`.
# Named explicitly rather than as "-1" so the test still describes the right
# starting point after another revision lands on top of head.
PRE_M9B_REVISION = "ddb267266304"


@pytest.fixture(scope="module")
def alembic_env():
    """A migrated test database and the alembic Config pointed at it.

    Module-scoped, so `pytest.MonkeyPatch.context()` rather than the
    function-scoped `monkeypatch` fixture. `require_test_database_url` compares
    database *names*, which is what stops `downgrade base` from being aimed at the
    development database by a URL that is spelled differently but resolves to it.
    """
    test_url = require_test_database_url("m9b role authority")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "DATABASE_URL", test_url)

        cfg = Config("alembic.ini")
        command.downgrade(cfg, "base")

        yield cfg, test_url

        command.downgrade(cfg, "base")


def _session_factory(test_url: str):
    engine = create_async_engine(test_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Case 5 — assertion 9
# ---------------------------------------------------------------------------


def test_role_name_is_readable_from_a_real_async_session(alembic_env):
    """`UserSession.role` is built from `user.role.name` — the load must be eager.

    A lazy load on an async session raises `MissingGreenlet` at attribute access
    instead of emitting SQL, so this is the only kind of test that can catch a
    missing `selectinload`. The companion probe below shows it has teeth.
    """
    cfg, test_url = alembic_env
    command.upgrade(cfg, "head")

    keycloak_id = f"kc-eager-{uuid.uuid4()}"

    async def run():
        engine, factory = _session_factory(test_url)
        try:
            async with factory() as session:
                session.add(
                    User(
                        id=uuid.uuid4(),
                        email=f"{keycloak_id}@whitecape.fr",
                        keycloak_id=keycloak_id,
                        role_id=SEED_ROLE_DEVELOPER_ID,
                        role_source=ROLE_SOURCE_ADMIN_ASSIGNED,
                    )
                )
                await session.commit()

            # A *fresh* session: nothing in the identity map, so `role` has to be
            # loaded by the query itself. Reusing the seeding session would leave
            # the relationship warm and make this pass for the wrong reason.
            async with factory() as session:
                service = KeycloakAuthService(db=session, redis=MockRedis())
                result = await service._lazy_sync_user(
                    {"sub": keycloak_id, "email": f"{keycloak_id}@whitecape.fr"}
                )
                await session.commit()
                return result
        finally:
            await engine.dispose()

    result = asyncio.run(run())

    # No MissingGreenlet, and the name came from the seeded Role row.
    assert result.role == "developer"
    assert result.role_confirmed is True


def test_a_lazy_role_load_really_does_raise_missinggreenlet(alembic_env):
    """Two-sided probe: without the eager option the same read fails.

    Not a contract test case. It exists so assertion 9 cannot pass vacuously — if
    lazy loading happened to work here, the test above would prove nothing about
    `selectinload`.
    """
    cfg, test_url = alembic_env
    command.upgrade(cfg, "head")

    keycloak_id = f"kc-lazy-{uuid.uuid4()}"

    async def run():
        engine, factory = _session_factory(test_url)
        try:
            async with factory() as session:
                session.add(
                    User(
                        id=uuid.uuid4(),
                        email=f"{keycloak_id}@whitecape.fr",
                        keycloak_id=keycloak_id,
                        role_id=SEED_ROLE_DEVELOPER_ID,
                        role_source=ROLE_SOURCE_ADMIN_ASSIGNED,
                    )
                )
                await session.commit()

            async with factory() as session:
                # Deliberately no .options(selectinload(User.role)) — the exact
                # statement production had before M9b.
                found = await session.execute(
                    select(User).where(User.keycloak_id == keycloak_id)
                )
                user = found.scalar_one()
                with pytest.raises(MissingGreenlet):
                    _ = user.role.name
        finally:
            await engine.dispose()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Case 6 — assertion 10
# ---------------------------------------------------------------------------


def test_migration_backfills_role_source_and_drops_its_enum_type(alembic_env):
    """Upgrade backfills existing rows; downgrade removes the column AND the type.

    The final re-upgrade is the part that actually proves the DROP TYPE: alembic
    emits no `DROP TYPE` of its own, so a downgrade that only drops the column
    leaves `role_source` in `pg_type` and the next `upgrade head` dies with
    `type "role_source" already exists` — the hazard documented at
    alembic/versions/ddb267266304_initial_schema.py:238-243.
    """
    cfg, test_url = alembic_env

    # Start from the pre-M9b schema so there is a row that predates the column.
    command.downgrade(cfg, "base")
    command.upgrade(cfg, PRE_M9B_REVISION)

    legacy_id = uuid.uuid4()

    async def seed_pre_m9b_user():
        engine, _ = _session_factory(test_url)
        try:
            async with engine.begin() as conn:
                assert not await _column_exists(conn), (
                    "user.role_source already exists at the pre-M9b revision — "
                    "PRE_M9B_REVISION is wrong and this test proves nothing"
                )
                await conn.execute(
                    text(
                        'INSERT INTO "user" '
                        "(id, email, keycloak_id, role_id, is_admin, is_owner) "
                        "VALUES (:id, :email, :kc, :role_id, false, false)"
                    ),
                    {
                        "id": legacy_id,
                        "email": "legacy-row@whitecape.fr",
                        "kc": f"kc-legacy-{legacy_id}",
                        "role_id": SEED_ROLE_DEVELOPER_ID,
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_pre_m9b_user())

    # --- upgrade: the column arrives NOT NULL and the existing row is backfilled
    command.upgrade(cfg, "head")

    async def read_after_upgrade():
        engine, _ = _session_factory(test_url)
        try:
            async with engine.connect() as conn:
                value = (
                    await conn.execute(
                        text('SELECT role_source FROM "user" WHERE id = :id'),
                        {"id": legacy_id},
                    )
                ).scalar()
                meta = (
                    await conn.execute(
                        text(
                            "SELECT is_nullable, column_default FROM information_schema.columns "
                            "WHERE table_name = 'user' AND column_name = 'role_source'"
                        )
                    )
                ).fetchone()
            return value, meta
        finally:
            await engine.dispose()

    value, meta = asyncio.run(read_after_upgrade())

    assert value == ROLE_SOURCE_DEFAULT, "pre-existing row was not backfilled"
    assert meta is not None, "role_source column absent after upgrade"
    is_nullable, column_default = meta
    assert is_nullable == "NO"
    # Rendered by postgres as `'default'::role_source`.
    assert column_default is not None and ROLE_SOURCE_DEFAULT in column_default

    # --- downgrade: both the column and the native type must be gone
    command.downgrade(cfg, PRE_M9B_REVISION)

    async def read_after_downgrade():
        engine, _ = _session_factory(test_url)
        try:
            async with engine.connect() as conn:
                column_left = await _column_exists(conn)
                type_left = (
                    await conn.execute(
                        text("SELECT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'role_source')")
                    )
                ).scalar()
            return column_left, type_left
        finally:
            await engine.dispose()

    column_left, type_left = asyncio.run(read_after_downgrade())

    assert not column_left, "downgrade left user.role_source behind"
    assert not type_left, "downgrade dropped the column but left the role_source enum type"

    # --- the round trip that a leftover type would break
    command.upgrade(cfg, "head")


async def _column_exists(conn) -> bool:
    return bool(
        (
            await conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'user' AND column_name = 'role_source')"
                )
            )
        ).scalar()
    )
