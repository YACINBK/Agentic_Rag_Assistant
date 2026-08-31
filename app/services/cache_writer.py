"""M10 — semantic cache write-back (the writer node 0 has been waiting for).

Runs AFTER the faithfulness verdict, only on the FAITHFUL generated path (never
on cache hits — nothing to learn — and never on DIRECT responses — nothing
grounded to serve later). The route calls this once the answer is settled.

Two payload amendments beyond CLAUDE.md §9's list, both pre-registered:
- admin_only mirrors user_is_admin at write time (D5): node 0's read path
  appends admin_only==False for non-admins on every collection, so a §9-exact
  payload would give non-admins zero hits forever.
- document_ids (D37): a re-ingestion that drops a region leaves those chunk
  ids absent from Stage E's report, so intersection-only invalidation misses
  entries grounded solely in that region. Carrying document_ids lets
  invalidation key on the document instead.

Failure is soft and loud: the answer has already been delivered to the user, so
a cache write failure logs and returns rather than breaking the stream.
"""

from __future__ import annotations

import time
import uuid

from app.core.logging import get_logger
from app.core.services.embedder import BaseEmbedder
from app.core.services.vector_store import BaseVectorStore
from app.core.settings import Settings

logger = get_logger(__name__)


async def write_cache_entry(
    *,
    query: str,
    answer: str,
    chunks: list[dict],
    citations: list[dict],
    user_role: str,
    user_is_admin: bool,
    embedder: BaseEmbedder,
    vector_store: BaseVectorStore,
    settings: Settings,
) -> None:
    """Persist one faithful (query, answer) pair into the semantic cache.

    `citations` ride along so a cache hit renders the answer with the same
    hover cards, sources and images as the full pipeline — a text-only entry
    would serve raw [N] markers with no figures (found live 2026-08-31).
    """
    try:
        vector = await embedder.embed_single(query)
        chunk_ids = [c["chunk_id"] for c in chunks if c.get("chunk_id")]
        document_ids = sorted({c["document_id"] for c in chunks if c.get("document_id")})
        await vector_store.upsert(
            collection=settings.QDRANT_CACHE_COLLECTION,
            points=[
                {
                    "id": str(uuid.uuid4()),
                    "vector": vector,
                    "payload": {
                        "query_text": query,
                        "answer_text": answer,
                        "chunk_ids": chunk_ids,
                        "document_ids": document_ids,
                        "citations": citations,
                        "role": user_role,
                        "admin_only": user_is_admin,
                        "created_at": time.time(),
                        "ttl_hours": settings.CACHE_TTL_HOURS,
                    },
                }
            ],
        )
        logger.info(
            "cache_entry_written",
            role=user_role,
            admin_only=user_is_admin,
            chunks=len(chunk_ids),
        )
    except Exception as exc:  # noqa: BLE001 — the answer is already served
        logger.warning("cache_write_failed", error=str(exc))
