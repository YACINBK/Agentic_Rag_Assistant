"""Integration tests for M8b — what a real Postgres proves that a fake session cannot.

**Scope, stated plainly.** These tests do NOT exercise the routes. There is no
TestClient here, no `require_admin`, no 409, no Celery enqueue — all of that is
covered in `tests/unit/test_m8b_admin_mutations.py`, which drives the real handlers
through fake collaborators.

What this file is for is the one thing a fake session structurally cannot reach:
`test_delete_document_integration` proves the `document_image` FK's
`ON DELETE CASCADE` (`app/core/models/document_image.py:25`) actually fires, which
is the second half of contract assertion 2. The unit suite proves the route issues
no statement of its own for those rows; this proves the database removes them anyway.

`test_reingest_integration` is weaker, and known to be: it asserts that assigning
`ingestion_status` and `last_ingested_at` and committing persists them, which is a
property of SQLAlchemy rather than of the route. It is kept as a schema smoke test
and logged as owed work — the version that would earn its place drives
`POST /reingest` through a TestClient against this database.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.models.document import Document
from app.core.models.document_image import DocumentImage
from app.core.settings import settings
from tests.conftest import require_test_database_url

pytestmark = pytest.mark.asyncio
pytestmark.asyncio_mode = "auto"

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
    test_url = require_test_database_url("m8b mutations")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(settings, "DATABASE_URL", test_url)
        config = Config("alembic.ini")
        _reset_schema(test_url)
        command.upgrade(config, "head")
        yield test_url
        command.downgrade(config, "base")
async def _setup_session(test_url: str):
    engine = create_async_engine(test_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory, engine


async def _seed_owner_row(factory) -> uuid.UUID:
    """A User row the Document FK can point at. Role first — role_id is FK too."""
    from app.core.models.role import Role
    from app.core.models.user import User

    async with factory() as session:
        role = Role(name=f"m8b-role-{uuid.uuid4().hex[:8]}")
        session.add(role)
        await session.flush()
        user = User(
            email=f"m8b-{uuid.uuid4().hex[:8]}@whitecape.fr",
            keycloak_id=uuid.uuid4().hex,
            role_id=role.id,
        )
        session.add(user)
        await session.commit()
        return user.id


async def test_delete_document_integration(alembic_env):
    test_url = alembic_env
    factory, engine = await _setup_session(test_url)
    try:
        owner_id = await _seed_owner_row(factory)
        async with factory() as session:
            # Seed a document. source_path and uploaded_by are NOT NULL with an
            # FK — omitting either aborts the insert before the cascade is ever
            # reached, which is what this test exists to prove.
            doc = Document(
                original_filename="test.pdf",
                source_path="/uploads/m8b-integration-test.pdf",
                category="technical",
                restricted=False,
                doc_hash=f"hash-{uuid.uuid4().hex[:12]}",
                uploaded_by=owner_id,
            )
            session.add(doc)
            await session.commit()
            doc_id = doc.id

            # Seed an image
            img = DocumentImage(document_id=doc_id, image_id="img123")
            session.add(img)
            await session.commit()

        # Trigger DELETE logic
        async with factory() as session:
            await session.execute(delete(Document).where(Document.id == doc_id))
            await session.commit()

        async with factory() as session:
            doc_exists = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
            img_exists = (await session.execute(select(DocumentImage).where(DocumentImage.document_id == doc_id))).scalar_one_or_none()
            assert doc_exists is None
            assert img_exists is None
    finally:
        await engine.dispose()


async def test_reingest_integration(alembic_env):
    test_url = alembic_env
    factory, engine = await _setup_session(test_url)
    try:
        owner_id = await _seed_owner_row(factory)
        async with factory() as session:
            doc = Document(
                original_filename="test.pdf",
                source_path="/uploads/m8b-integration-test.pdf",
                category="technical",
                restricted=False,
                doc_hash=f"hash-{uuid.uuid4().hex[:12]}",
                uploaded_by=owner_id,
                ingestion_status="done",
            )
            session.add(doc)
            await session.commit()
            doc_id = doc.id
        # Simulate reingest logic
        async with factory() as session:
            doc = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
            doc.ingestion_status = "pending"
            doc.last_ingested_at = None
            await session.commit()
            
        async with factory() as session:
            doc = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
            assert doc.ingestion_status == "pending"
            assert doc.last_ingested_at is None
    finally:
        await engine.dispose()
