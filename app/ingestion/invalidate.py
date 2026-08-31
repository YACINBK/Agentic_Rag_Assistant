"""Ingestion Stage G — semantic cache invalidation (CLAUDE.md §9).

Runs after Stage E has written the document's new points and before the document
is marked `done`. Deletes every `semantic_cache` entry whose `chunk_ids` overlap
the document's current point ids.

Why intersection and not a diff (M6 D17): point ids are positional, derived as
`uuid5(_NAMESPACE, f"{document_id}:{page_id}:{chunk_index}")` at
`app/ingestion/index.py:249`. An *edited* chunk therefore keeps its id, so the
diff of old against new ids is empty exactly when invalidation matters most.
§9's wording ("its old chunk IDs are replaced with new ones") reads as though a
diff would work; it is the trap.

The cost of intersection is over-invalidation: re-ingesting a document drops every
cache entry citing any part of it, changed or not. That is the correct direction —
over-invalidation costs one cache miss, under-invalidation serves an answer
grounded in content that no longer exists. §9's claim that invalidation "is
precise" describes an ideal this mechanism cannot reach under positional ids; the
mechanism is right, the sentence is aspirational.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.services.vector_store import BaseVectorStore
from app.core.settings import settings

logger = get_logger(__name__)

# The cache payload field listing the point ids an answer was grounded in (§9).
CACHE_CHUNK_IDS_KEY = "chunk_ids"


@dataclass(frozen=True)
class InvalidationResult:
    collection: str  # the collection actually targeted
    chunk_ids_matched: int  # len(chunk_ids) the delete was issued for; 0 == no call
    entries_deleted: int  # always -1 — the store protocol reports no count


async def invalidate_cache_for_chunks(
    chunk_ids: list[str],
    *,
    vector_store: BaseVectorStore,
) -> InvalidationResult:
    """Delete every semantic_cache entry whose chunk_ids overlap the given ids.

    There is deliberately no `collection` parameter. The target is read from
    settings here, so no caller can aim this at QDRANT_COLLECTION and delete the
    corpus instead of the cache.
    """
    # Read at call time, not import time: a module-level constant would freeze the
    # value before a test (or a deployment override) could set it.
    collection = settings.QDRANT_CACHE_COLLECTION

    if not chunk_ids:
        # A real path, not a defensive one: a document with no BookStack markers
        # indexes to zero chunks and IndexResult.chunk_ids is []. A full result is
        # still returned — never None, never a bare early return — so the caller
        # handles both branches identically.
        logger.info("cache_invalidation_no_chunks", collection=collection)
        return InvalidationResult(
            collection=collection,
            chunk_ids_matched=0,
            entries_deleted=-1,
        )

    # Passed through verbatim: no sort, no dedup, no re-derivation from
    # document_id. Any transform here would mean the ids being invalidated are no
    # longer the ids Stage E actually wrote.
    await vector_store.delete_by_any(collection, CACHE_CHUNK_IDS_KEY, chunk_ids)

    logger.info(
        "cache_invalidated",
        collection=collection,
        chunk_ids_matched=len(chunk_ids),
    )
    return InvalidationResult(
        collection=collection,
        chunk_ids_matched=len(chunk_ids),
        # -1, not 0: delete_by_any returns None by protocol, so the number of
        # entries actually removed is unknowable here. 0 would read as
        # "invalidation ran, nothing was stale". Follows IndexResult.points_purged.
        entries_deleted=-1,
    )
