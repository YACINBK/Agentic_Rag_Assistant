"""Ingestion Stage F — orchestration and persisted document lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from app.core.logging import get_logger
from app.core.services.document_repository import BaseDocumentRepository
from app.core.services.embedder import BaseEmbedder
from app.core.services.llm import BaseLLMService
from app.core.services.storage import BaseStorage
from app.core.services.vector_store import BaseVectorStore
from app.ingestion.chunk import chunk_bookstack_html
from app.ingestion.enrich import enrich_chunks
from app.ingestion.extract import extract_bookstack_html
from app.ingestion.index import index_chunks
from app.ingestion.invalidate import invalidate_cache_for_chunks

logger = get_logger(__name__)


class DocumentNotFoundError(Exception):
    """Raised when a requested document row does not exist."""


class IngestionError(Exception):
    """Raised with stage context when ingestion fails after it starts."""

    def __init__(self, message: str, *, document_id: str, stage: str) -> None:
        super().__init__(message)
        self.document_id = document_id
        self.stage = stage


@dataclass(frozen=True)
class IngestionResult:
    document_id: str
    chunk_count: int
    image_count: int
    skipped_images: int


@lru_cache(maxsize=1)
def _bge_length_fn() -> Callable[[str], int]:
    """Load and memoise BGE-M3's tokenizer only when production chunking needs it."""
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-m3")
    return lambda text: len(tokenizer.encode(text))


async def orchestrate_ingestion(
    document_id: str,
    *,
    allowed_roles: list[str],
    repository: BaseDocumentRepository,
    storage: BaseStorage,
    llm: BaseLLMService,
    embedder: BaseEmbedder,
    vector_store: BaseVectorStore,
    enricher_model: str,
    length_fn: Callable[[str], int] | None = None,
) -> IngestionResult:
    """Run Stages A -> B -> C -> E and persist the resulting document state."""
    if not document_id or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")

    record = await repository.get(document_id)
    await repository.mark_running(document_id)

    stage = "extract"
    try:
        raw_bytes = await storage.read(record.source_path)
        raw_html = raw_bytes.decode("utf-8")
        extract_result = await extract_bookstack_html(
            raw_html,
            document_id=document_id,
            storage=storage,
        )

        stage = "chunk"
        chunks = chunk_bookstack_html(
            extract_result.cleaned_html,
            length_fn=length_fn if length_fn is not None else _bge_length_fn(),
        )

        stage = "enrich"
        enriched = await enrich_chunks(chunks, llm=llm, model=enricher_model)

        stage = "index"
        index_result = await index_chunks(
            enriched,
            document_id=document_id,
            original_filename=record.original_filename,
            category=record.category,
            allowed_roles=allowed_roles,
            restricted=record.restricted,
            doc_hash=record.doc_hash,
            embedder=embedder,
            vector_store=vector_store,
        )

        # Stage G — §9: stale cache entries die after the new points are written
        # and before the document is marked done. Inside the try on purpose: a
        # failure here must mark the document failed rather than reach `done`,
        # because "marked done with a stale entry still servable" is silent and
        # "marked failed" is loud and re-triggerable (M6 assertions 9-10).
        stage = "invalidate"
        await invalidate_cache_for_chunks(
            index_result.chunk_ids,
            vector_store=vector_store,
        )

        stage = "repository"
        await repository.replace_images(
            document_id,
            [image.image_id for image in extract_result.images],
        )
        await repository.mark_done(document_id, chunk_count=index_result.points_written)
    except Exception as exc:
        try:
            await repository.mark_failed(document_id)
        except Exception as mark_failed_exc:
            logger.error(
                "ingestion_mark_failed_error",
                document_id=document_id,
                stage=stage,
                error=str(mark_failed_exc),
            )
        raise IngestionError(
            f"Ingestion failed for document {document_id!r} during {stage}",
            document_id=document_id,
            stage=stage,
        ) from exc

    return IngestionResult(
        document_id=document_id,
        chunk_count=index_result.points_written,
        image_count=len(extract_result.images),
        skipped_images=extract_result.skipped,
    )
