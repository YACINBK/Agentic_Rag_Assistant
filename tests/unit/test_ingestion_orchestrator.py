"""Unit contract tests for Ingestion Stage F orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.core.services.document_repository import BaseDocumentRepository, DocumentRecord
from app.core.settings import settings
from app.ingestion.extract import ExtractedImage, ExtractResult
from app.ingestion.index import IndexResult
from app.ingestion.orchestrator import (
    DocumentNotFoundError,
    IngestionError,
    IngestionResult,
    orchestrate_ingestion,
)
from app.services.storage import LocalFileStorage
from tests.conftest import MockEmbedder, MockLLMService, MockVectorStore


def ws_len(text: str) -> int:
    return len(text.split())


class FakeDocumentRepository(BaseDocumentRepository):
    def __init__(
        self,
        record: DocumentRecord,
        events: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.record = record
        self.events = events if events is not None else []
        self.missing = False

    async def get(self, document_id: str) -> DocumentRecord:
        self.events.append(("get", document_id))
        if self.missing:
            raise DocumentNotFoundError(document_id)
        return self.record

    async def mark_running(self, document_id: str) -> None:
        self.events.append(("mark_running", document_id))

    async def mark_done(self, document_id: str, *, chunk_count: int) -> None:
        self.events.append(("mark_done", document_id, chunk_count))

    async def mark_failed(self, document_id: str) -> None:
        self.events.append(("mark_failed", document_id))

    async def replace_images(self, document_id: str, image_ids: list[str]) -> None:
        self.events.append(("replace_images", document_id, image_ids))


@pytest.fixture
def orchestration_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    source = tmp_path / "source.html"
    source.write_text("<html><body>source</body></html>", encoding="utf-8")
    document_id = "11111111-1111-4111-8111-111111111111"
    record = DocumentRecord(
        id=document_id,
        source_path=str(source),
        original_filename="askgo.html",
        category="technical",
        restricted=False,
        doc_hash="a" * 64,
    )
    return {
        "document_id": document_id,
        "repository": FakeDocumentRepository(record),
        "storage": LocalFileStorage(),
        "llm": MockLLMService(),
        "embedder": MockEmbedder(),
        "vector_store": MockVectorStore(),
    }


def _extract_result(image_ids: list[str] | None = None, *, skipped: int = 2) -> ExtractResult:
    images = [
        ExtractedImage(
            image_id=image_id,
            source_path=f"/derived/{image_id}.png",
            mime_type="image/png",
            byte_size=10,
            needs_caption=False,
            occurrences=1,
        )
        for image_id in (image_ids or [])
    ]
    return ExtractResult(
        cleaned_html="<h1 id='page-1'>Page</h1><p>clean</p>",
        cleaned_path="/derived/clean.html",
        images=images,
        skipped=skipped,
    )


def _index_result(document_id: str, points_written: int = 3) -> IndexResult:
    return IndexResult(
        document_id=document_id,
        points_written=points_written,
        points_purged=-1,
        chunk_ids=[f"chunk-{index}" for index in range(points_written)],
        batches=1,
        vector_dim=1024,
    )


async def _call(env, *, allowed_roles: list[str] | None = None):
    return await orchestrate_ingestion(
        env["document_id"],
        allowed_roles=allowed_roles if allowed_roles is not None else ["all"],
        repository=env["repository"],
        storage=env["storage"],
        llm=env["llm"],
        embedder=env["embedder"],
        vector_store=env["vector_store"],
        enricher_model="test-model",
        length_fn=ws_len,
    )


@pytest.mark.asyncio
async def test_happy_path_full_pipeline(orchestration_env):
    env = orchestration_env
    extract = AsyncMock(return_value=_extract_result(["img-a"], skipped=4))
    chunk = Mock(return_value=["chunk"])
    enrich = AsyncMock(return_value=["enriched"])
    index = AsyncMock(return_value=_index_result(env["document_id"], 7))

    with (
        patch("app.ingestion.orchestrator.extract_bookstack_html", extract),
        patch("app.ingestion.orchestrator.chunk_bookstack_html", chunk),
        patch("app.ingestion.orchestrator.enrich_chunks", enrich),
        patch("app.ingestion.orchestrator.index_chunks", index),
    ):
        result = await _call(env)

    assert result == IngestionResult(env["document_id"], 7, 1, 4)
    assert env["vector_store"].delete_calls == []
    assert env["vector_store"].upsert_calls == []
    assert env["embedder"].calls == []
    assert env["llm"].calls == []


@pytest.mark.asyncio
async def test_call_order_extract_chunk_enrich_index(orchestration_env):
    env = orchestration_env
    calls: list[str] = []

    async def extract(*args, **kwargs):
        calls.append("extract")
        return _extract_result()

    def chunk(*args, **kwargs):
        calls.append("chunk")
        return ["chunk"]

    async def enrich(*args, **kwargs):
        calls.append("enrich")
        return ["enriched"]

    async def index(*args, **kwargs):
        calls.append("index")
        return _index_result(env["document_id"])

    with (
        patch("app.ingestion.orchestrator.extract_bookstack_html", extract),
        patch("app.ingestion.orchestrator.chunk_bookstack_html", chunk),
        patch("app.ingestion.orchestrator.enrich_chunks", enrich),
        patch("app.ingestion.orchestrator.index_chunks", index),
    ):
        await _call(env)

    assert calls == ["extract", "chunk", "enrich", "index"]


@pytest.mark.asyncio
async def test_mark_running_before_any_stage(orchestration_env):
    env = orchestration_env
    events = env["repository"].events

    async def extract(*args, **kwargs):
        events.append(("extract",))
        return _extract_result()

    with (
        patch("app.ingestion.orchestrator.extract_bookstack_html", extract),
        patch("app.ingestion.orchestrator.chunk_bookstack_html", Mock(return_value=[])),
        patch("app.ingestion.orchestrator.enrich_chunks", AsyncMock(return_value=[])),
        patch(
            "app.ingestion.orchestrator.index_chunks",
            AsyncMock(return_value=_index_result(env["document_id"], 0)),
        ),
    ):
        await _call(env)

    assert events.index(("mark_running", env["document_id"])) < events.index(("extract",))


@pytest.mark.asyncio
async def test_mark_done_with_correct_chunk_count(orchestration_env):
    env = orchestration_env
    with (
        patch(
            "app.ingestion.orchestrator.extract_bookstack_html",
            AsyncMock(return_value=_extract_result()),
        ),
        patch("app.ingestion.orchestrator.chunk_bookstack_html", Mock(return_value=[])),
        patch("app.ingestion.orchestrator.enrich_chunks", AsyncMock(return_value=[])),
        patch(
            "app.ingestion.orchestrator.index_chunks",
            AsyncMock(return_value=_index_result(env["document_id"], 11)),
        ),
    ):
        await _call(env)

    assert ("mark_done", env["document_id"], 11) in env["repository"].events
    assert env["repository"].events[-1] == ("mark_done", env["document_id"], 11)


@pytest.mark.asyncio
async def test_replace_images_called_with_correct_ids(orchestration_env):
    env = orchestration_env
    image_ids = ["first-image", "second-image"]
    with (
        patch(
            "app.ingestion.orchestrator.extract_bookstack_html",
            AsyncMock(return_value=_extract_result(image_ids)),
        ),
        patch("app.ingestion.orchestrator.chunk_bookstack_html", Mock(return_value=[])),
        patch("app.ingestion.orchestrator.enrich_chunks", AsyncMock(return_value=[])),
        patch(
            "app.ingestion.orchestrator.index_chunks",
            AsyncMock(return_value=_index_result(env["document_id"], 0)),
        ),
    ):
        await _call(env)

    assert ("replace_images", env["document_id"], image_ids) in env["repository"].events


@pytest.mark.asyncio
async def test_failure_in_enrich_marks_failed_and_chains_exception(orchestration_env):
    env = orchestration_env
    original = RuntimeError("boom")
    with (
        patch(
            "app.ingestion.orchestrator.extract_bookstack_html",
            AsyncMock(return_value=_extract_result()),
        ),
        patch("app.ingestion.orchestrator.chunk_bookstack_html", Mock(return_value=["chunk"])),
        patch("app.ingestion.orchestrator.enrich_chunks", AsyncMock(side_effect=original)),
        patch("app.ingestion.orchestrator.index_chunks", AsyncMock()),
        pytest.raises(IngestionError) as exc_info,
    ):
        await _call(env)

    assert exc_info.value.stage == "enrich"
    assert exc_info.value.__cause__ is original
    assert env["repository"].events.count(("mark_failed", env["document_id"])) == 1
    assert not any(event[0] == "mark_done" for event in env["repository"].events)


@pytest.mark.asyncio
async def test_failure_in_index_marks_failed_and_chains_exception(orchestration_env):
    env = orchestration_env
    original = RuntimeError("boom")
    roles = ["all", "qa_engineer", "all"]
    index = AsyncMock(side_effect=original)
    with (
        patch(
            "app.ingestion.orchestrator.extract_bookstack_html",
            AsyncMock(return_value=_extract_result()),
        ),
        patch("app.ingestion.orchestrator.chunk_bookstack_html", Mock(return_value=["chunk"])),
        patch("app.ingestion.orchestrator.enrich_chunks", AsyncMock(return_value=["enriched"])),
        patch("app.ingestion.orchestrator.index_chunks", index),
        pytest.raises(IngestionError) as exc_info,
    ):
        await _call(env, allowed_roles=roles)

    assert exc_info.value.stage == "index"
    assert exc_info.value.__cause__ is original
    assert index.await_args is not None
    assert index.await_args.kwargs["allowed_roles"] is roles
    assert env["repository"].events.count(("mark_failed", env["document_id"])) == 1
    assert not any(event[0] == "mark_done" for event in env["repository"].events)


@pytest.mark.asyncio
async def test_missing_document_propagates_without_status_writes(orchestration_env):
    env = orchestration_env
    env["repository"].missing = True

    with pytest.raises(DocumentNotFoundError):
        await _call(env)

    names = [event[0] for event in env["repository"].events]
    assert names == ["get"]
