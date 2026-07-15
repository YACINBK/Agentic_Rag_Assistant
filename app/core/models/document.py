import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.core.models.base import Base

DocumentCategory = Enum(
    "technical", "quality", "projects", "company",
    name="document_category",
)

IngestionStatus = Enum(
    "pending", "running", "done", "failed",
    name="ingestion_status",
)


class Document(Base):
    __tablename__ = "document"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str] = mapped_column(DocumentCategory, nullable=False)
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    doc_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id"), nullable=False
    )
    last_ingested_at: Mapped[str | None] = mapped_column(nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    ingestion_status: Mapped[str] = mapped_column(
        IngestionStatus, default="pending"
    )
