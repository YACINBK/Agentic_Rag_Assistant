"""SQLAlchemy implementation of ingestion document persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models.document import Document
from app.core.models.document_image import DocumentImage
from app.core.services.document_repository import BaseDocumentRepository, DocumentRecord
from app.ingestion.orchestrator import DocumentNotFoundError


class SqlAlchemyDocumentRepository(BaseDocumentRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, document_id: str) -> DocumentRecord:
        document = await self._find(document_id)
        return DocumentRecord(
            id=str(document.id),
            source_path=document.source_path,
            original_filename=document.original_filename,
            category=document.category,
            restricted=document.restricted,
            doc_hash=document.doc_hash,
        )

    async def mark_running(self, document_id: str) -> None:
        document = await self._find(document_id)
        document.ingestion_status = "running"
        await self._session.commit()

    async def mark_done(self, document_id: str, *, chunk_count: int) -> None:
        document = await self._find(document_id)
        document.ingestion_status = "done"
        document.chunk_count = chunk_count
        document.last_ingested_at = datetime.now(timezone.utc)
        await self._session.commit()

    async def mark_failed(self, document_id: str) -> None:
        await self._session.rollback()
        document = await self._find(document_id)
        document.ingestion_status = "failed"
        await self._session.commit()

    async def replace_images(self, document_id: str, image_ids: list[str]) -> None:
        await self._session.execute(
            delete(DocumentImage).where(DocumentImage.document_id == self._as_uuid(document_id))
        )
        unique_ids = list(dict.fromkeys(image_ids))
        self._session.add_all(
            [
                DocumentImage(document_id=self._as_uuid(document_id), image_id=image_id)
                for image_id in unique_ids
            ]
        )
        await self._session.commit()

    async def _find(self, document_id: str) -> Document:
        try:
            parsed_id = self._as_uuid(document_id)
        except ValueError as exc:
            raise DocumentNotFoundError(f"Document {document_id!r} was not found") from exc
        result = await self._session.execute(select(Document).where(Document.id == parsed_id))
        document = result.scalar_one_or_none()
        if document is None:
            raise DocumentNotFoundError(f"Document {document_id!r} was not found")
        return document

    @staticmethod
    def _as_uuid(document_id: str) -> uuid.UUID:
        return uuid.UUID(document_id)
