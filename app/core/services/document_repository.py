"""Abstract persistence seam for ingestion document state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentRecord:
    id: str
    source_path: str
    original_filename: str
    category: str
    restricted: bool
    doc_hash: str


class BaseDocumentRepository(ABC):
    @abstractmethod
    async def get(self, document_id: str) -> DocumentRecord:
        """Fetch a document or raise DocumentNotFoundError."""
        ...

    @abstractmethod
    async def mark_running(self, document_id: str) -> None:
        """Set ingestion_status to running."""
        ...

    @abstractmethod
    async def mark_done(self, document_id: str, *, chunk_count: int) -> None:
        """Set completion state, count, and ingestion timestamp."""
        ...

    @abstractmethod
    async def mark_failed(self, document_id: str) -> None:
        """Set ingestion_status to failed without changing completion metadata."""
        ...

    @abstractmethod
    async def replace_images(self, document_id: str, image_ids: list[str]) -> None:
        """Replace the document's image junction rows with the unique input IDs."""
        ...
