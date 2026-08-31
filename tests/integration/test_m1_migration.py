"""M1 — integration tests against real PostgreSQL.

Covers assertions 4, 5, 6, 7, 8, 9 from the contract. Requires TEST_DATABASE_URL.
All tests are sync functions (not async def) — see trap 1 in the contract.
"""

import asyncio
import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.settings import settings

# Seeded Role IDs from the migration (alembic/versions/ddb267266304_initial_schema.py)
# These are hardcoded constants that must match exactly across all environments.
ROLE_DEVELOPER_ID = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000001")
ROLE_QA_ENGINEER_ID = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000002")


@pytest.fixture(scope="module")
def alembic_config():
    """Module-scoped fixture: test database, migration runner, cleanup on teardown.

    Traps addressed:
    - Trap 1: This fixture is sync (not async), so alembic upgrade/downgrade work.
    - Trap 2: Monkeypatches settings.DATABASE_URL via MonkeyPatch.context().
    - Trap 3: Guards against dev database explicitly.
    """
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL not set — integration tests skipped")

    # Trap 3: fail loudly if TEST_DATABASE_URL equals the dev database
    if test_url == str(settings.DATABASE_URL):
        pytest.fail(
            "TEST_DATABASE_URL must NOT equal settings.DATABASE_URL — "
            "downgrade base would destroy the development database"
        )

    # Trap 2: monkeypatch settings.DATABASE_URL so alembic/env.py reads the test URL
    # Module-scoped fixture requires MonkeyPatch.context(), not the function-scoped fixture
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "DATABASE_URL", test_url)

        cfg = Config("alembic.ini")

        # Start clean: downgrade to base if any schema exists
        command.downgrade(cfg, "base")

        yield cfg

        # Teardown: leave the database at base so re-runs start clean
        command.downgrade(cfg, "base")


def test_upgrade_creates_all_tables_and_round_trips(alembic_config):
    """Assertion 4: upgrade creates all 6 tables.
    Assertion 5: upgrade → downgrade → upgrade succeeds (enum types are dropped).
    """
    # First upgrade
    command.upgrade(alembic_config, "head")

    # Check table names
    async def check_tables():
        engine = create_async_engine(str(settings.DATABASE_URL))
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' ORDER BY tablename"
                )
            )
            tables = {row[0] for row in result}
        await engine.dispose()
        return tables

    tables = asyncio.run(check_tables())
    assert tables == {
        "alembic_version",
        "audit_log",
        "document",
        "document_image",
        "escalation_event",
        "role",
        "user",
    }

    # Round-trip: downgrade → upgrade again
    # A downgrade that leaves enum types behind makes the second upgrade fail
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")  # assertion 5: this must succeed


def test_single_owner_index_is_partial(alembic_config):
    """Assertion 6: idx_single_owner exists as a partial unique index."""
    command.upgrade(alembic_config, "head")

    async def check_index():
        engine = create_async_engine(str(settings.DATABASE_URL))
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE indexname = 'idx_single_owner'"
                )
            )
            row = result.fetchone()
        await engine.dispose()
        return row

    row = asyncio.run(check_index())
    assert row is not None, "idx_single_owner not found"
    indexdef = row[1]
    assert "WHERE" in indexdef, "idx_single_owner is not a partial index"


def test_owner_without_admin_rejected(alembic_config):
    """Assertion 7: INSERT with is_owner=true, is_admin=false raises IntegrityError."""
    command.upgrade(alembic_config, "head")

    # Trap 4: bind UUIDs as uuid.UUID objects, not strings
    user_id = uuid.uuid4()

    async def insert_user(is_admin: bool, is_owner: bool):
        engine = create_async_engine(str(settings.DATABASE_URL))
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO "user" '
                    "(id, email, keycloak_id, role_id, is_admin, is_owner) "
                    "VALUES (:id, :email, :keycloak_id, :role_id, :is_admin, :is_owner)"
                ),
                {
                    "id": user_id,
                    "email": "test@example.com",
                    "keycloak_id": "kc_test",
                    "role_id": ROLE_DEVELOPER_ID,  # use a seeded role
                    "is_admin": is_admin,
                    "is_owner": is_owner,
                },
            )
        await engine.dispose()

    # is_owner=true, is_admin=false must raise
    with pytest.raises(IntegrityError) as exc_info:
        asyncio.run(insert_user(is_admin=False, is_owner=True))
    assert "ck_owner_implies_admin" in str(exc_info.value)

    # is_owner=true, is_admin=true must succeed
    asyncio.run(insert_user(is_admin=True, is_owner=True))


def test_role_seed(alembic_config):
    """Assertion 8: upgrade seeds 2 roles with correct names and IDs.
    Assertion 9: downgrade deletes the 2 seeded roles.
    """
    command.upgrade(alembic_config, "head")

    async def query_roles():
        engine = create_async_engine(str(settings.DATABASE_URL))
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT id, name FROM role ORDER BY name"))
            rows = result.fetchall()
        await engine.dispose()
        return rows

    rows = asyncio.run(query_roles())
    assert len(rows) == 2
    names = {row[1] for row in rows}
    assert names == {"developer", "qa_engineer"}

    # Check IDs match the constants
    ids = {row[0] for row in rows}
    assert ids == {ROLE_DEVELOPER_ID, ROLE_QA_ENGINEER_ID}

    # Downgrade and assert the table is gone
    command.downgrade(alembic_config, "base")

    async def check_table_gone():
        engine = create_async_engine(str(settings.DATABASE_URL))
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'role'"
                    ")"
                )
            )
            exists = result.scalar()
        await engine.dispose()
        return exists

    table_exists = asyncio.run(check_table_gone())
    assert not table_exists
