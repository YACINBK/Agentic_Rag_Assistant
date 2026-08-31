"""Celery entry point for one ingestion-orchestrator attempt.

Why this module builds its own engine instead of importing `async_session` from
`app.core.models.base`: that engine is constructed at *import* time with SQLAlchemy's
default `AsyncAdaptedQueuePool` (size 5), while this task body is sync and drives the
async orchestrator through `asyncio.run()` — which opens a fresh event loop per call
and closes it on return. An asyncpg connection is bound to the loop that opened it,
and a pooled connection outlives the `async with` block that used it. So run #1 would
leave loop-L1 connections sitting in the pool, and run #2 would be handed one whose
loop is already closed (`RuntimeError: Event loop is closed`). The first document in a
worker process would ingest cleanly and every one after it would fail — a defect no
single-run test can see. Celery's prefork model compounds it: a forked child inherits
the parent's pool along with its open sockets.

So the engine is built *inside* the coroutine (therefore inside the loop that will use
it), with NullPool so no connection can outlive that loop, and is disposed before the
loop closes. `tests/integration/test_image_serving_route.py` documents the same
workaround for the same reason, and the Stage F integration tests build their engine
this way too. The cost is one TCP connect per ingestion — against a job measured in
tens of minutes, it rounds to zero.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.settings import settings
from app.ingestion.orchestrator import orchestrate_ingestion
from app.services.document_repository import SqlAlchemyDocumentRepository
from app.services.embedder import OllamaEmbedder
from app.services.llm import LiteLLMService
from app.services.storage import LocalFileStorage
from app.services.vector_store import QdrantVectorStore
from app.tasks.worker import celery_app


async def _run_ingestion(document_id: str, allowed_roles: list[str]) -> dict[str, str | int]:
    """Own the engine lifecycle for exactly one attempt (see module docstring)."""
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            result = await orchestrate_ingestion(
                document_id,
                allowed_roles=allowed_roles,
                repository=SqlAlchemyDocumentRepository(session),
                storage=LocalFileStorage(),
                llm=LiteLLMService(),
                embedder=OllamaEmbedder(),
                vector_store=QdrantVectorStore(),
                enricher_model=settings.ENRICHER_MODEL,
            )
        return {"document_id": result.document_id, "chunk_count": result.chunk_count}
    finally:
        # Inside the coroutine, so it runs while the loop is still alive — dispose()
        # is itself awaitable and cannot be deferred past asyncio.run()'s return.
        await engine.dispose()


@celery_app.task(
    bind=True,
    # No automatic retry. Stage C preserves no partial progress — it is strictly
    # sequential, and one exception discards the whole document's enrichment — so a
    # retry re-runs tens of minutes of LLM calls from zero. Re-running is *safe*
    # (Stage E purges by document_id and derives point ids deterministically, and
    # replace_images is delete-then-insert), it is merely expensive; the decision to
    # spend that belongs to an admin re-trigger, not to a silent backoff.
    max_retries=0,
    # The soft limit raises SoftTimeLimitExceeded *inside* the task. It derives from
    # Exception, so the orchestrator's own except path catches it and still writes
    # ingestion_status='failed' — which is the entire point: without it, a hung LLM or
    # embedder call strands the row at 'running' forever. The hard limit is a SIGKILL
    # backstop, set deliberately later so that 'failed' write has room to land.
    soft_time_limit=settings.INGESTION_SOFT_TIME_LIMIT,
    time_limit=settings.INGESTION_TIME_LIMIT,
)
def ingest_document(self, document_id: str, allowed_roles: list[str]) -> dict[str, str | int]:
    """Run one ingestion attempt; raised failures are reported by Celery."""
    return asyncio.run(_run_ingestion(document_id, allowed_roles))
