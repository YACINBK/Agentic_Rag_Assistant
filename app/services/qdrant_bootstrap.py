"""Qdrant bootstrap — idempotent collection + payload-index creation.

Nothing else in the codebase creates a Qdrant collection: QdrantVectorStore
assumes the collection already exists. This module is the one operational concern
that makes a fresh Qdrant usable, and it is safe to run on every startup.

Two silent failure modes it prevents (CLAUDE.md §8, contract M2):
  1. Wrong vector size — Qdrant fixes dimensionality at creation; a wrong size
     rejects every point at ingestion while the collection looks healthy.
  2. Missing payload index — an unindexed filter field does not error, it just
     full-scans on every query. The §5 security filter still returns correct
     results, so the only symptom is latency that grows with the corpus.

Forbidden (contract §Forbidden): never delete/recreate a collection, never write
a point, never swallow errors. If a collection exists with the wrong vector size,
this module leaves it alone rather than destroying the corpus on restart.
"""

from __future__ import annotations

import asyncio

from qdrant_client import AsyncQdrantClient, models

from app.core.logging import get_logger
from app.core.settings import settings

logger = get_logger(__name__)


# Payload indexes per collection (contract Outputs table). The field name maps to
# the Qdrant payload schema type. Every field here is one a query filters on.
_DOCUMENTS_INDEXES: dict[str, models.PayloadSchemaType] = {
    "allowed_roles": models.PayloadSchemaType.KEYWORD,  # §5 subject-matter scope
    "admin_only": models.PayloadSchemaType.BOOL,        # §5 privilege tier
    "document_id": models.PayloadSchemaType.KEYWORD,    # re-ingestion / delete by doc
}

_CACHE_INDEXES: dict[str, models.PayloadSchemaType] = {
    "role": models.PayloadSchemaType.KEYWORD,           # §9 cache lookup filter
    "admin_only": models.PayloadSchemaType.BOOL,        # D5 — else leaks restricted answer
    "created_at": models.PayloadSchemaType.FLOAT,       # §9 TTL purge range filter
    "chunk_ids": models.PayloadSchemaType.KEYWORD,      # M6 invalidation match
}


async def _ensure_one_collection(
    client: AsyncQdrantClient,
    name: str,
    indexes: dict[str, models.PayloadSchemaType],
) -> None:
    """Create the collection if missing, then add only the missing payload indexes.

    Idempotent: an existing collection is never recreated (that would destroy the
    corpus), and an index already present in the collection's payload_schema is
    never re-created. A collection that exists but is missing one index still gets
    that one index — the contract's assertion 8.
    """
    if not await client.collection_exists(name):
        await client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(
                size=settings.EMBEDDING_DIM,
                distance=models.Distance.COSINE,
            ),
        )
        logger.info("qdrant_collection_created", collection=name, size=settings.EMBEDDING_DIM)
        existing_fields: set[str] = set()
    else:
        info = await client.get_collection(name)
        existing_fields = set((info.payload_schema or {}).keys())
        logger.info(
            "qdrant_collection_present", collection=name, indexed_fields=sorted(existing_fields)
        )

    for field_name, schema in indexes.items():
        if field_name in existing_fields:
            continue
        await client.create_payload_index(
            collection_name=name,
            field_name=field_name,
            field_schema=schema,
        )
        logger.info("qdrant_payload_index_created", collection=name, field=field_name)


async def ensure_collections(client: AsyncQdrantClient | None = None) -> None:
    """Idempotently create the documents and semantic_cache collections and their
    payload indexes. Safe to call on every startup.

    An injected client is used as-is and is not closed here. When ``client`` is
    None a client is constructed from settings and closed before returning.
    """
    owns_client = client is None
    if client is None:
        client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)

    try:
        await _ensure_one_collection(client, settings.QDRANT_COLLECTION, _DOCUMENTS_INDEXES)
        await _ensure_one_collection(
            client, settings.QDRANT_CACHE_COLLECTION, _CACHE_INDEXES
        )
    finally:
        if owns_client:
            await client.close()


def main() -> None:
    """CLI entry point: `python -m app.services.qdrant_bootstrap`."""
    asyncio.run(ensure_collections())


if __name__ == "__main__":
    main()
