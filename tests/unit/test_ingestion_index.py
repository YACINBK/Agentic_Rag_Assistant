"""Unit tests for ingestion Stage E — Indexer (`app/ingestion/index.py`).

Service doubles come from `tests/conftest.py` — `MockEmbedder` records each batch
in `.calls`, `MockVectorStore` records `.upsert_calls` / `.delete_calls`. Two tests
need a local subclass (see the contract's Environment section): `OrderedStore` for
the shared delete-vs-upsert ordering log (test 5), and a count-mismatch embedder
(test 6, second case) — `MockEmbedder` returns one vector per text by construction,
so a count mismatch is unreachable through it.

`make_chunk` / `make_state` are deliberately NOT reused: they are query-time shapes.
Stage E's input is `EnrichedChunk`, built here by a small local factory.
"""

from __future__ import annotations

import pytest

from app.core.settings import settings
from app.ingestion.enrich import EnrichedChunk
from app.ingestion.index import IndexingError, index_chunks
from tests.conftest import MockEmbedder, MockVectorStore

# ---------------------------------------------------------------------------
# Local input factory + subclasses (both named in the contract's Environment)
# ---------------------------------------------------------------------------


def _make_enriched(
    *,
    page_id: str,
    chunk_index: int,
    text: str = "Contenu de la section.",
    page_title: str = "Titre de la page",
    chapter_id: str = "chapter-1",
    chapter_title: str = "Chapitre 1",
    section_path: str = "Transversal > Section",
    anchor: str = "bkmrk-a",
    token_count: int = 42,
    image_refs: list | None = None,
    table_count: int = 0,
    related_anchors: list[str] | None = None,
    is_generalized_procedure: bool = False,
    applies_to: list[str] | None = None,
    contextual_header: str = "Objectif de la section.",
) -> EnrichedChunk:
    return EnrichedChunk(
        text=text,
        page_id=page_id,
        page_title=page_title,
        chapter_id=chapter_id,
        chapter_title=chapter_title,
        section_path=section_path,
        anchor=anchor,
        chunk_index=chunk_index,
        token_count=token_count,
        image_refs=image_refs if image_refs is not None else [],
        table_count=table_count,
        related_anchors=related_anchors if related_anchors is not None else [],
        is_generalized_procedure=is_generalized_procedure,
        applies_to=applies_to if applies_to is not None else [],
        contextual_header=contextual_header,
    )


def _embed_text(chunk: EnrichedChunk) -> str:
    """Independent replica of the module's embed-text rule, for cross-checking."""
    return f"{chunk.contextual_header}\n\n{chunk.text}" if chunk.contextual_header else chunk.text


class OrderedStore(MockVectorStore):
    """Records delete and upsert in ONE shared ordered list — conftest's two
    separate lists cannot express cross-method ordering (assertion 7)."""

    def __init__(self):
        super().__init__()
        self.log: list[tuple[str, dict]] = []

    async def upsert(self, collection, points):
        self.log.append(("upsert", {"collection": collection, "points": points}))
        await super().upsert(collection, points)

    async def delete_by_filter(self, collection, filter_conditions):
        self.log.append(("delete", {"collection": collection, "filter": filter_conditions}))
        await super().delete_by_filter(collection, filter_conditions)


class _ShortCountEmbedder(MockEmbedder):
    """Returns a single vector regardless of how many texts were passed, to reach
    the count-mismatch branch (assertion 8, second case)."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.1] * self._dim]


# Common kwargs so each test states only what it is exercising.
_BASE = dict(
    document_id="doc-1",
    original_filename="askgo-621x-fr.html",
    category="technical",
    doc_hash="a" * 64,
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_one_point_per_chunk_in_order():
    """Assertion 1: one point per chunk, in input order, one upsert call."""
    chunks = [
        _make_enriched(page_id="page-1", chunk_index=0, text="Texte zéro."),
        _make_enriched(page_id="page-1", chunk_index=1, text="Texte un."),
        _make_enriched(page_id="page-2", chunk_index=0, text="Texte deux."),
    ]
    embedder, store = MockEmbedder(), MockVectorStore()

    result = await index_chunks(
        chunks,
        allowed_roles=["all"],
        restricted=False,
        embedder=embedder,
        vector_store=store,
        **_BASE,
    )

    assert len(store.upsert_calls) == 1
    points = store.upsert_calls[0]["points"]
    assert len(points) == 3
    for i, point in enumerate(points):
        assert point["payload"]["chunk_index"] == chunks[i].chunk_index
        assert point["payload"]["text"] == chunks[i].text
    assert result.points_written == 3
    assert result.chunk_ids == [p["id"] for p in points]
    assert len(set(result.chunk_ids)) == 3


async def test_header_embedded_but_not_in_payload():
    """Assertions 2, 3: header prefixes the embedded text but never the payload text."""
    header = "Objectif : créer une commande."
    chunks = [
        _make_enriched(page_id="p", chunk_index=0, text="Corps A.", contextual_header=header),
        _make_enriched(page_id="p", chunk_index=1, text="Corps B.", contextual_header=""),
    ]
    embedder, store = MockEmbedder(), MockVectorStore()

    await index_chunks(
        chunks,
        allowed_roles=["all"],
        restricted=False,
        embedder=embedder,
        vector_store=store,
        **_BASE,
    )

    # Single batch → one recorded call carrying both embed-texts, in order.
    assert embedder.calls == [[f"{header}\n\nCorps A.", "Corps B."]]

    points = store.upsert_calls[0]["points"]
    assert points[0]["payload"]["text"] == "Corps A."
    assert points[1]["payload"]["text"] == "Corps B."
    for point in points:
        assert header not in point["payload"]["text"]


async def test_privilege_and_scope_written_verbatim():
    """Assertions 4, 6: admin_only mirrors restricted; allowed_roles and applies_to
    stay separate keys."""
    chunks = [
        _make_enriched(page_id="p", chunk_index=0, applies_to=["Commandes"]),
        _make_enriched(page_id="p", chunk_index=1, applies_to=[]),
    ]
    embedder, store = MockEmbedder(), MockVectorStore()

    await index_chunks(
        chunks,
        allowed_roles=["developer"],
        restricted=True,
        embedder=embedder,
        vector_store=store,
        **_BASE,
    )

    points = store.upsert_calls[0]["points"]
    for point in points:
        assert point["payload"]["admin_only"] is True
        assert point["payload"]["allowed_roles"] == ["developer"]
    assert points[0]["payload"]["applies_to"] == ["Commandes"]
    assert "Commandes" not in points[0]["payload"]["allowed_roles"]


async def test_reserved_role_name_rejected_before_any_write():
    """Assertion 5: a reserved role (any case) or an empty list raises ValueError
    before anything is embedded, purged, or upserted."""
    chunks = [
        _make_enriched(page_id="p", chunk_index=0),
        _make_enriched(page_id="p", chunk_index=1),
    ]
    embedder, store = MockEmbedder(), MockVectorStore()

    with pytest.raises(ValueError):
        await index_chunks(
            chunks,
            allowed_roles=["developer", "Admin"],
            restricted=False,
            embedder=embedder,
            vector_store=store,
            **_BASE,
        )

    # Nothing embedded, nothing purged, nothing written — validation precedes the
    # purge, so a rejected call leaves the collection untouched.
    assert embedder.calls == []
    assert store.upsert_calls == []
    assert store.delete_calls == []

    with pytest.raises(ValueError):
        await index_chunks(
            chunks,
            allowed_roles=[],
            restricted=False,
            embedder=embedder,
            vector_store=store,
            **_BASE,
        )


async def test_purge_precedes_upsert_and_targets_the_document():
    """Assertion 7: delete_by_filter runs before the first upsert, targeting the
    document in the target collection."""
    chunks = [
        _make_enriched(page_id="p", chunk_index=0),
        _make_enriched(page_id="p", chunk_index=1),
    ]
    embedder, store = MockEmbedder(), OrderedStore()

    await index_chunks(
        chunks,
        document_id="doc-abc",
        original_filename="askgo-621x-fr.html",
        category="technical",
        doc_hash="a" * 64,
        allowed_roles=["all"],
        restricted=False,
        embedder=embedder,
        vector_store=store,
    )

    assert store.log[0][0] == "delete"
    assert store.log[0][1]["filter"]["document_id"] == "doc-abc"
    assert store.log[0][1]["collection"] == settings.QDRANT_COLLECTION
    # No upsert entry precedes the delete.
    upsert_positions = [i for i, e in enumerate(store.log) if e[0] == "upsert"]
    assert upsert_positions and min(upsert_positions) > 0


async def test_wrong_vector_dim_raises_before_upsert():
    """Assertion 8: wrong dim OR wrong vector count raises IndexingError, no upsert."""
    chunks = [
        _make_enriched(page_id="p", chunk_index=0),
        _make_enriched(page_id="p", chunk_index=1),
    ]

    # Case 1 — 512-length vectors instead of EMBEDDING_DIM.
    store = MockVectorStore()
    with pytest.raises(IndexingError):
        await index_chunks(
            chunks,
            allowed_roles=["all"],
            restricted=False,
            embedder=MockEmbedder(vector_dim=512),
            vector_store=store,
            **_BASE,
        )
    assert store.upsert_calls == []

    # Case 2 — one vector returned for two texts.
    store2 = MockVectorStore()
    with pytest.raises(IndexingError):
        await index_chunks(
            chunks,
            allowed_roles=["all"],
            restricted=False,
            embedder=_ShortCountEmbedder(),
            vector_store=store2,
            **_BASE,
        )
    assert store2.upsert_calls == []


async def test_batching_covers_every_chunk_once():
    """Assertion 9: batch_size=2 over 5 chunks → batches 2,2,1 covering all texts."""
    chunks = [_make_enriched(page_id="p", chunk_index=i, text=f"T{i}.") for i in range(5)]
    embedder, store = MockEmbedder(), MockVectorStore()

    result = await index_chunks(
        chunks,
        allowed_roles=["all"],
        restricted=False,
        embedder=embedder,
        vector_store=store,
        batch_size=2,
        **_BASE,
    )

    assert [len(batch) for batch in embedder.calls] == [2, 2, 1]
    flattened = [text for batch in embedder.calls for text in batch]
    assert flattened == [_embed_text(c) for c in chunks]
    assert result.batches == 3
    assert result.points_written == 5


async def test_empty_input_is_a_noop_that_still_purges():
    """Assertion 10: empty input → no embed, no upsert, but purge still runs once."""
    embedder, store = MockEmbedder(), MockVectorStore()

    result = await index_chunks(
        [],
        allowed_roles=["all"],
        restricted=False,
        embedder=embedder,
        vector_store=store,
        **_BASE,
    )

    assert result.points_written == 0
    assert result.chunk_ids == []
    assert result.batches == 0
    assert embedder.calls == []
    assert store.upsert_calls == []
    assert len(store.delete_calls) == 1
