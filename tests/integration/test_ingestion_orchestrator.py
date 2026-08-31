"""Stage F integration tests against a real, explicitly gated PostgreSQL database."""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.models.document import Document
from app.core.models.document_image import DocumentImage
from app.core.settings import settings
from app.ingestion.extract import ExtractedImage, ExtractResult
from app.ingestion.index import IndexResult
from app.ingestion.orchestrator import IngestionError, orchestrate_ingestion
from app.services.document_repository import SqlAlchemyDocumentRepository
from app.services.storage import LocalFileStorage
from tests.conftest import MockEmbedder, MockLLMService, MockVectorStore

ROLE_DEVELOPER_ID = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000001")


@pytest.fixture(scope="module")
def database_url():
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL not set — integration tests skipped")
    if test_url == str(settings.DATABASE_URL):
        pytest.fail(
            "TEST_DATABASE_URL must NOT equal settings.DATABASE_URL — "
            "downgrade base would destroy the development database"
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "DATABASE_URL", test_url)
        cfg = Config("alembic.ini")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        yield test_url
        command.downgrade(cfg, "base")


def _fixtures(document_id: str, image_ids: list[str], points_written: int):
    images = [
        ExtractedImage(
            image_id=image_id,
            source_path=f"/derived/{image_id}.png",
            mime_type="image/png",
            byte_size=10,
            needs_caption=False,
            occurrences=1,
        )
        for image_id in image_ids
    ]
    extract_result = ExtractResult(
        cleaned_html="<h1 id='page-1'>Page</h1><p>clean</p>",
        cleaned_path="/derived/clean.html",
        images=images,
        skipped=0,
    )
    index_result = IndexResult(
        document_id=document_id,
        points_written=points_written,
        points_purged=-1,
        chunk_ids=[f"chunk-{index}" for index in range(points_written)],
        batches=1,
        vector_dim=1024,
    )
    return extract_result, index_result


async def _run_case(
    database_url: str,
    source_path: str,
    *,
    image_ids: list[str],
    points_written: int,
    fail_index: bool = False,
):
    engine = create_async_engine(database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    document_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async with factory() as seed_session:
        await seed_session.execute(
            text(
                'INSERT INTO "user" '
                "(id, email, keycloak_id, role_id, is_admin, is_owner) "
                "VALUES (:id, :email, :keycloak_id, :role_id, false, false)"
            ),
            {
                "id": user_id,
                "email": f"orchestrator-{user_id}@test.invalid",
                "keycloak_id": f"kc-{user_id}",
                "role_id": ROLE_DEVELOPER_ID,
            },
        )
        await seed_session.execute(
            text(
                "INSERT INTO document "
                "(id, source_path, original_filename, category, restricted, doc_hash, "
                "uploaded_by, chunk_count, ingestion_status) "
                "VALUES (:id, :source_path, :filename, 'technical', false, :doc_hash, "
                ":uploaded_by, 0, 'pending')"
            ),
            {
                "id": document_id,
                "source_path": source_path,
                "filename": "askgo.html",
                "doc_hash": uuid.uuid4().hex + uuid.uuid4().hex,
                "uploaded_by": user_id,
            },
        )
        await seed_session.commit()

    extract_result, index_result = _fixtures(str(document_id), image_ids, points_written)
    roles = ["all"]
    index_mock = AsyncMock(
        side_effect=RuntimeError("index failed") if fail_index else None,
        return_value=None if fail_index else index_result,
    )

    async with factory() as orchestration_session:
        with (
            patch(
                "app.ingestion.orchestrator.extract_bookstack_html",
                AsyncMock(return_value=extract_result),
            ),
            patch("app.ingestion.orchestrator.chunk_bookstack_html", Mock(return_value=["chunk"])),
            patch(
                "app.ingestion.orchestrator.enrich_chunks",
                AsyncMock(return_value=["enriched"]),
            ),
            patch("app.ingestion.orchestrator.index_chunks", index_mock),
        ):
            if fail_index:
                with pytest.raises(IngestionError):
                    await orchestrate_ingestion(
                        str(document_id),
                        allowed_roles=roles,
                        repository=SqlAlchemyDocumentRepository(orchestration_session),
                        storage=LocalFileStorage(),
                        llm=MockLLMService(),
                        embedder=MockEmbedder(),
                        vector_store=MockVectorStore(),
                        enricher_model="test-model",
                        length_fn=lambda value: len(value.split()),
                    )
            else:
                await orchestrate_ingestion(
                    str(document_id),
                    allowed_roles=roles,
                    repository=SqlAlchemyDocumentRepository(orchestration_session),
                    storage=LocalFileStorage(),
                    llm=MockLLMService(),
                    embedder=MockEmbedder(),
                    vector_store=MockVectorStore(),
                    enricher_model="test-model",
                    length_fn=lambda value: len(value.split()),
                )

    assert index_mock.await_args is not None
    assert index_mock.await_args.kwargs["allowed_roles"] is roles

    async with factory() as query_session:
        document = await query_session.get(Document, document_id)
        images = (
            await query_session.execute(
                select(DocumentImage.image_id)
                .where(DocumentImage.document_id == document_id)
                .order_by(DocumentImage.image_id)
            )
        ).scalars().all()

    await engine.dispose()
    return document, list(images)


@pytest.mark.integration
def test_integration_happy_path_persists_document_and_images(database_url, tmp_path):
    source = tmp_path / "happy.html"
    source.write_text("<html>source</html>", encoding="utf-8")
    image_ids = ["a" * 64, "b" * 64]

    document, images = asyncio.run(
        _run_case(
            database_url,
            str(source),
            image_ids=image_ids,
            points_written=5,
        )
    )

    assert document is not None
    assert document.ingestion_status == "done"
    assert document.chunk_count == 5
    assert document.last_ingested_at is not None
    assert images == image_ids


@pytest.mark.integration
def test_integration_failure_leaves_status_failed_not_running(database_url, tmp_path):
    source = tmp_path / "failure.html"
    source.write_text("<html>source</html>", encoding="utf-8")

    document, images = asyncio.run(
        _run_case(
            database_url,
            str(source),
            image_ids=["c" * 64],
            points_written=0,
            fail_index=True,
        )
    )

    assert document is not None
    assert document.ingestion_status == "failed"
    assert images == []
