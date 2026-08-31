"""M6 — semantic cache invalidation (Stage G). All mocked, no live Qdrant.

Covers the contract's 10 assertions across 8 cases. The store double is
`MockVectorStore` from tests/conftest.py everywhere except two places the contract
names explicitly: test 7's shared ordering recorder (one log, shared between the
repository and the store — two separate lists cannot express the interleaving, the
same reason `ingestion_index.md` assertion 7 needed it) and test 8's raising store.

Tests 2 and 3 inject a local recording Qdrant client instead of using
MockVectorStore, because the assertion is about the *constructed Qdrant filter* and
MockVectorStore never builds one.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, Mock, patch

import pytest
from qdrant_client import models

from app.core.services.document_repository import BaseDocumentRepository, DocumentRecord
from app.core.services.vector_store import BaseVectorStore
from app.core.settings import settings
from app.ingestion.extract import ExtractResult
from app.ingestion.index import IndexResult
from app.ingestion.invalidate import InvalidationResult, invalidate_cache_for_chunks
from app.ingestion.orchestrator import IngestionError, orchestrate_ingestion
from app.services.storage import LocalFileStorage
from app.services.vector_store import QdrantVectorStore
from tests.conftest import MockEmbedder, MockLLMService, MockVectorStore

# ---------------------------------------------------------------------------
# 1 — the ABC gap M6 owns (D2)
# ---------------------------------------------------------------------------


def test_abc_declares_delete_by_any() -> None:
    """Assertion 1. Abstract, not merely present: a concrete-with-`...` body would
    let a subclass silently inherit a no-op invalidation."""
    assert "delete_by_any" in BaseVectorStore.__abstractmethods__

    # eval_str resolves the PEP 563 string annotations this module and the ABC both
    # carry, so the assertion reads against real objects rather than "None".
    signature = inspect.signature(BaseVectorStore.delete_by_any, eval_str=True)

    assert list(signature.parameters) == ["self", "collection", "key", "values"]
    assert signature.parameters["collection"].annotation is str
    assert signature.parameters["key"].annotation is str
    assert signature.parameters["values"].annotation == list[str]
    assert signature.return_annotation is None


# ---------------------------------------------------------------------------
# 2, 3 — the Qdrant filter the ABC method builds
# ---------------------------------------------------------------------------


class _RecordingQdrantClient:
    """Captures `delete` kwargs verbatim, following the client-injection pattern of
    tests/unit/test_m0_privilege_dimension.py:45.

    Injected rather than letting QdrantVectorStore build its own client: the real
    constructor performs a live version handshake against QDRANT_HOST, which the
    contract's Environment table forbids in the unit suite and which would make
    these two cases depend on a running container.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def delete(self, **kwargs):
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_qdrant_delete_by_any_uses_match_any() -> None:
    """Assertion 2. MatchAny, one FieldCondition, wrapped in a FilterSelector."""
    client = _RecordingQdrantClient()
    store = QdrantVectorStore(client=client)

    await store.delete_by_any("semantic_cache", "chunk_ids", ["a", "b"])

    assert len(client.calls) == 1
    kwargs = client.calls[0]
    assert kwargs["collection_name"] == "semantic_cache"

    selector = kwargs["points_selector"]
    assert isinstance(selector, models.FilterSelector)

    # Exactly one condition: a second would AND an extra constraint onto the delete
    # and could narrow it to nothing.
    assert len(selector.filter.must) == 1
    condition = selector.filter.must[0]
    assert condition.key == "chunk_ids"

    # MatchAny, not MatchValue — the D2 distinction this whole method exists for.
    assert isinstance(condition.match, models.MatchAny)
    assert not isinstance(condition.match, models.MatchValue)
    assert condition.match.any == ["a", "b"]


@pytest.mark.asyncio
async def test_qdrant_delete_by_any_empty_values_is_noop() -> None:
    """Assertion 3. No client call at all on empty values.

    Filter(must=[]) matches every point in the collection, so a refactor that drops
    the guard and keeps building the filter turns "invalidate nothing" into "wipe
    the cache". Asserting on the client, not on a return value, is what catches it.
    """
    client = _RecordingQdrantClient()
    store = QdrantVectorStore(client=client)

    await store.delete_by_any("semantic_cache", "chunk_ids", [])

    assert client.calls == []


# ---------------------------------------------------------------------------
# 4, 5, 6 — Stage G itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_targets_cache_collection_with_chunk_ids_key() -> None:
    """Assertions 4, 5. The collection is read from settings, never a parameter."""
    store = MockVectorStore()

    result = await invalidate_cache_for_chunks(["id-1", "id-2"], vector_store=store)

    assert len(store.delete_by_any_calls) == 1
    call = store.delete_by_any_calls[0]
    assert call["collection"] == settings.QDRANT_CACHE_COLLECTION
    assert call["key"] == "chunk_ids"

    # Assertion 4's entailment: the corpus collection is never touched, by this
    # method or by any other store method.
    assert call["collection"] != settings.QDRANT_COLLECTION
    assert store.delete_calls == []
    assert store.upsert_calls == []

    assert result.collection == settings.QDRANT_CACHE_COLLECTION
    assert result.entries_deleted == -1


@pytest.mark.asyncio
async def test_invalidate_passes_chunk_ids_verbatim() -> None:
    """Assertion 6. Unsorted, with a duplicate — a sort or a dedup here would mean
    the ids are being re-derived rather than passed through from Stage E."""
    store = MockVectorStore()
    chunk_ids = ["b", "a", "b"]

    result = await invalidate_cache_for_chunks(chunk_ids, vector_store=store)

    assert store.delete_by_any_calls[0]["values"] == ["b", "a", "b"]
    assert result.chunk_ids_matched == 3


@pytest.mark.asyncio
async def test_invalidate_empty_chunk_ids_issues_no_call() -> None:
    """The zero-chunk document, which occurs in the real corpus (a file with no
    BookStack markers). Still a full result — never None, never a bare return."""
    store = MockVectorStore()

    result = await invalidate_cache_for_chunks([], vector_store=store)

    assert store.delete_by_any_calls == []
    assert isinstance(result, InvalidationResult)
    assert result.chunk_ids_matched == 0
    # -1, not 0: 0 would read as "invalidation ran, nothing was stale".
    assert result.entries_deleted == -1
    assert result.collection == settings.QDRANT_CACHE_COLLECTION


# ---------------------------------------------------------------------------
# 7, 8, 9, 10 — the orchestrator call site
# ---------------------------------------------------------------------------


class _RecordingRepository(BaseDocumentRepository):
    """Repository writing to a log SHARED with the store (contract test 7).

    Two separate lists could each be internally ordered and still not show whether
    invalidation landed between index and mark_done. One log is the only shape that
    observes the interleaving.
    """

    def __init__(self, record: DocumentRecord, log: list[tuple[object, ...]]) -> None:
        self.record = record
        self.log = log

    async def get(self, document_id: str) -> DocumentRecord:
        self.log.append(("get", document_id))
        return self.record

    async def mark_running(self, document_id: str) -> None:
        self.log.append(("mark_running", document_id))

    async def mark_done(self, document_id: str, *, chunk_count: int) -> None:
        self.log.append(("mark_done", document_id, chunk_count))

    async def mark_failed(self, document_id: str) -> None:
        self.log.append(("mark_failed", document_id))

    async def replace_images(self, document_id: str, image_ids: list[str]) -> None:
        self.log.append(("replace_images", document_id, image_ids))


class _RecordingStore(MockVectorStore):
    """Store writing invalidation into the same shared log."""

    def __init__(self, log: list[tuple[object, ...]]) -> None:
        super().__init__()
        self.log = log

    async def delete_by_any(self, collection: str, key: str, values: list[str]) -> None:
        self.log.append(("invalidate", collection, key, values))
        await super().delete_by_any(collection, key, values)


class _RaisingStore(MockVectorStore):
    """Store whose invalidation fails (contract test 8)."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def delete_by_any(self, collection: str, key: str, values: list[str]) -> None:
        raise self._error


def _record(tmp_path) -> DocumentRecord:
    source = tmp_path / "source.html"
    source.write_text("<html><body>source</body></html>", encoding="utf-8")
    return DocumentRecord(
        id="11111111-1111-4111-8111-111111111111",
        source_path=str(source),
        original_filename="askgo.html",
        category="technical",
        restricted=False,
        doc_hash="a" * 64,
    )


def _stage_patches(index_result: IndexResult):
    """Stages A-E stubbed: this file tests the seam at G, not the stages before it."""
    extract = ExtractResult(
        cleaned_html="<h1 id='page-1'>Page</h1><p>clean</p>",
        cleaned_path="/derived/clean.html",
        images=[],
        skipped=0,
    )
    return (
        patch("app.ingestion.orchestrator.extract_bookstack_html", AsyncMock(return_value=extract)),
        patch("app.ingestion.orchestrator.chunk_bookstack_html", Mock(return_value=["chunk"])),
        patch("app.ingestion.orchestrator.enrich_chunks", AsyncMock(return_value=["enriched"])),
        patch("app.ingestion.orchestrator.index_chunks", AsyncMock(return_value=index_result)),
    )


async def _orchestrate(record: DocumentRecord, repository, store):
    return await orchestrate_ingestion(
        record.id,
        allowed_roles=["all"],
        repository=repository,
        storage=LocalFileStorage(),
        llm=MockLLMService(),
        embedder=MockEmbedder(),
        vector_store=store,
        enricher_model="test-model",
        length_fn=lambda text: len(text.split()),
    )


@pytest.mark.asyncio
async def test_orchestrator_invalidates_between_index_and_mark_done(tmp_path, monkeypatch) -> None:
    """Assertions 7, 8."""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    record = _record(tmp_path)
    log: list[tuple[object, ...]] = []
    repository = _RecordingRepository(record, log)
    store = _RecordingStore(log)

    chunk_ids = ["x", "y"]
    index_result = IndexResult(
        document_id=record.id,
        points_written=2,
        points_purged=-1,
        chunk_ids=chunk_ids,
        batches=1,
        vector_dim=1024,
    )

    async def index(*args, **kwargs):
        log.append(("index",))
        return index_result

    extract, chunk, enrich, _ = _stage_patches(index_result)
    with extract, chunk, enrich, patch("app.ingestion.orchestrator.index_chunks", index):
        await _orchestrate(record, repository, store)

    names = [entry[0] for entry in log]
    # Assertion 7 — one interval, stated positionally.
    assert names.index("index") < names.index("invalidate") < names.index("mark_done")

    # Assertion 8 — the object Stage E returned, not a re-derivation from
    # document_id. Identity, not equality: an equal-but-rebuilt list would mean the
    # ids came from somewhere other than the index result.
    invalidate_entry = next(entry for entry in log if entry[0] == "invalidate")
    assert invalidate_entry[3] is chunk_ids


@pytest.mark.asyncio
async def test_orchestrator_invalidation_failure_marks_failed_with_stage(
    tmp_path, monkeypatch
) -> None:
    """Assertions 9, 10. A document that indexed perfectly still fails here, on
    purpose: reaching `done` would leave the stale entry servable with no error and
    no log line, which is the exposure this module exists to close."""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    record = _record(tmp_path)
    log: list[tuple[object, ...]] = []
    repository = _RecordingRepository(record, log)
    original = RuntimeError("qdrant down")
    store = _RaisingStore(original)

    index_result = IndexResult(
        document_id=record.id,
        points_written=2,
        points_purged=-1,
        chunk_ids=["x", "y"],
        batches=1,
        vector_dim=1024,
    )

    extract, chunk, enrich, index = _stage_patches(index_result)
    with extract, chunk, enrich, index, pytest.raises(IngestionError) as exc_info:
        await _orchestrate(record, repository, store)

    assert exc_info.value.stage == "invalidate"
    assert exc_info.value.__cause__ is original
    assert ("mark_failed", record.id) in log
    assert not any(entry[0] == "mark_done" for entry in log)
