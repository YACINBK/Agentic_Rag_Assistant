"""Ingestion Stage E — Indexer.

The last ingestion stage and the only one that writes into the retrieval path.
Takes Stage C's ``EnrichedChunk`` list, embeds each chunk (header-prefixed) with
the injected embedder, and upserts one Qdrant point per chunk into the
``documents`` collection. It purges the document's existing points first, so the
collection reflects exactly this run (re-ingest of a shrunk document leaves no
orphans).

Boundaries (see contracts/ingestion_index.md "What this stage is NOT"):
Document row updates, document_image rows and cache invalidation are the
orchestrator's / M6's job. This stage returns counts; it never opens a DB session,
creates a collection, or talks to Qdrant/Ollama directly — everything goes through
the injected ``BaseEmbedder`` / ``BaseVectorStore`` protocols.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.services.embedder import BaseEmbedder
from app.core.services.vector_store import BaseVectorStore
from app.core.settings import settings
from app.ingestion.enrich import EnrichedChunk

logger = get_logger(__name__)

# RFC 4122 URL namespace — deterministic, positional point ids. Re-ingesting an
# unedited document rewrites the same points instead of accumulating duplicates.
_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

_VALID_CATEGORIES = frozenset({"technical", "quality", "projects", "company"})
# Flags, never roles (§2/§5). A chunk tagged with either matches no user.
_RESERVED_ROLES = frozenset({"admin", "owner"})


class IndexingError(Exception):
    """Raised when embedding or upsert fails, or when a vector's shape is wrong.

    One attempt, no retry — retry is the orchestrator's job. Carries the
    document_id and the batch index of the failing unit.
    """

    def __init__(
        self,
        message: str,
        *,
        document_id: str | None = None,
        batch_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.batch_index = batch_index


@dataclass(frozen=True)
class IndexResult:
    document_id: str
    points_written: int  # == len(chunks)
    points_purged: int  # pre-existing points deleted; -1 if the store can't report
    chunk_ids: list[str]  # point ids written, in input order — §9 cache needs these
    batches: int  # embed round trips actually made
    vector_dim: int  # dim actually returned by the embedder


def _embed_text(chunk: EnrichedChunk) -> str:
    """The string handed to the embedder: header-prefixed when the header is
    non-empty, the raw text alone otherwise (no trailing separator)."""
    header = chunk.contextual_header
    if header:
        return f"{header}\n\n{chunk.text}"
    return chunk.text


def _validate_inputs(
    *,
    document_id: str,
    original_filename: str,
    category: str,
    allowed_roles: list[str],
    doc_hash: str,
    batch_size: int,
) -> None:
    """Reject bad inputs before anything is purged or written. Order matters:
    a reserved role must raise before the purge (assertion 5)."""
    if not document_id or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")
    if not original_filename:
        raise ValueError("original_filename must be non-empty")
    if category not in _VALID_CATEGORIES:
        raise ValueError(f"category must be one of {sorted(_VALID_CATEGORIES)}, got {category!r}")
    if not allowed_roles:
        # An empty list would MatchAny([user_role, "all"]) against nothing —
        # the chunk is indexed, counted, reported as success, invisible to all.
        raise ValueError("allowed_roles must be non-empty")
    for role in allowed_roles:
        if not role:
            raise ValueError("allowed_roles entries must be non-empty")
        if role.lower() in _RESERVED_ROLES:
            raise ValueError(
                f"allowed_roles may not contain the reserved flag {role!r} "
                "(admin/owner are privilege flags, not roles)"
            )
    if not doc_hash:
        raise ValueError("doc_hash must be non-empty")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")


def _page_numbers(chunks: list[EnrichedChunk]) -> dict[str, int]:
    """Map each page_id to its 1-based ordinal in first-seen (document) order.
    page_number is NOT the digits of page_id — BookStack's ``page-1284`` is a
    database id, not a page number."""
    ordinals: dict[str, int] = {}
    for chunk in chunks:
        if chunk.page_id not in ordinals:
            ordinals[chunk.page_id] = len(ordinals) + 1
    return ordinals


def _build_payload(
    chunk: EnrichedChunk,
    *,
    document_id: str,
    original_filename: str,
    category: str,
    allowed_roles: list[str],
    restricted: bool,
    doc_hash: str,
    page_number: int,
) -> dict:
    """The payload written per point. New lists throughout — the caller's mutable
    lists (image_refs/related_anchors/applies_to) are never written through."""
    return {
        # --- §8 required set ---
        "text": chunk.text,  # UNPREFIXED — header lives in the vector only
        "document_id": document_id,
        "original_filename": original_filename,
        "category": category,
        "allowed_roles": list(allowed_roles),  # §5 dimension 1 — subject-matter scope
        "admin_only": restricted,  # §5 dimension 2 — privilege tier, verbatim
        "page_number": page_number,
        "chunk_index": chunk.chunk_index,
        "doc_hash": doc_hash,
        # --- BookStack provenance, read by the citation hover card ---
        "page_id": chunk.page_id,
        "page_title": chunk.page_title,
        "chapter_id": chunk.chapter_id,
        "chapter_title": chunk.chapter_title,
        "section_path": chunk.section_path,
        "anchor": chunk.anchor,
        "image_refs": [  # list[dict], never list[ImageRef]
            {"image_id": r.image_id, "anchor": r.anchor, "needs_caption": r.needs_caption}
            for r in chunk.image_refs
        ],
        "table_count": chunk.table_count,
        "related_anchors": list(chunk.related_anchors),
        # --- Stage C enrichment ---
        "applies_to": list(chunk.applies_to),  # module names — NOT roles, unindexed
        "contextual_header": chunk.contextual_header,  # stored for debugging, never rendered
        "is_generalized_procedure": chunk.is_generalized_procedure,
    }


async def index_chunks(
    chunks: list[EnrichedChunk],
    *,
    document_id: str,
    original_filename: str,
    category: str,
    allowed_roles: list[str],
    restricted: bool,
    doc_hash: str,
    embedder: BaseEmbedder,
    vector_store: BaseVectorStore,
    collection: str | None = None,
    batch_size: int = 32,
) -> IndexResult:
    """Embed and upsert one document's enriched chunks. Purges the document's
    existing points first, so the collection reflects exactly this run."""
    _validate_inputs(
        document_id=document_id,
        original_filename=original_filename,
        category=category,
        allowed_roles=allowed_roles,
        doc_hash=doc_hash,
        batch_size=batch_size,
    )

    target = collection or settings.QDRANT_COLLECTION
    expected_dim = settings.EMBEDDING_DIM

    # Purge first — even for empty input. Delete-then-write is what keeps a shrunk
    # re-ingest from leaving orphaned points behind. The protocol returns None, so
    # the deleted count is unknown: -1, distinct from 0 ("purge ran, nothing there").
    await vector_store.delete_by_filter(target, {"document_id": document_id})
    points_purged = -1

    if not chunks:
        return IndexResult(
            document_id=document_id,
            points_written=0,
            points_purged=points_purged,
            chunk_ids=[],
            batches=0,
            vector_dim=0,
        )

    page_numbers = _page_numbers(chunks)

    vectors: list[list[float]] = []
    batches = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        texts = [_embed_text(c) for c in batch]
        try:
            batch_vectors = await embedder.embed(texts)
        except Exception as exc:
            raise IndexingError(
                f"embedding failed for document {document_id!r}",
                document_id=document_id,
                batch_index=batches,
            ) from exc
        batches += 1
        if len(batch_vectors) != len(texts):
            raise IndexingError(
                f"embedder returned {len(batch_vectors)} vectors for {len(texts)} "
                f"texts in document {document_id!r}",
                document_id=document_id,
                batch_index=batches - 1,
            )
        for vec in batch_vectors:
            if len(vec) != expected_dim:
                raise IndexingError(
                    f"embedder returned dim {len(vec)}, expected {expected_dim} "
                    f"in document {document_id!r}",
                    document_id=document_id,
                    batch_index=batches - 1,
                )
        vectors.extend(batch_vectors)

    # Build every point before writing any — no partial-success, no orphan on a
    # late dim failure. A wrong dimension has already raised above, before this.
    points: list[dict] = []
    chunk_ids: list[str] = []
    for chunk, vector in zip(chunks, vectors):
        point_id = str(uuid.uuid5(_NAMESPACE, f"{document_id}:{chunk.page_id}:{chunk.chunk_index}"))
        payload = _build_payload(
            chunk,
            document_id=document_id,
            original_filename=original_filename,
            category=category,
            allowed_roles=allowed_roles,
            restricted=restricted,
            doc_hash=doc_hash,
            page_number=page_numbers[chunk.page_id],
        )
        points.append({"id": point_id, "vector": vector, "payload": payload})
        chunk_ids.append(point_id)

    try:
        await vector_store.upsert(target, points)
    except Exception as exc:
        raise IndexingError(
            f"upsert failed for document {document_id!r}",
            document_id=document_id,
        ) from exc

    logger.info(
        "index_chunks.done",
        document_id=document_id,
        points_written=len(points),
        batches=batches,
        collection=target,
    )
    return IndexResult(
        document_id=document_id,
        points_written=len(points),
        points_purged=points_purged,
        chunk_ids=chunk_ids,
        batches=batches,
        vector_dim=len(vectors[0]),
    )
