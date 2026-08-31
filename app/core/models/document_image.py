from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models.base import Base


class DocumentImage(Base):
    """Junction table tying extracted images back to their owning document.

    Written by the ingestion orchestrator (Stage E) after the Extractor (Stage A)
    produces the image list. Required for the image-serving route's auth check:
    without it an authenticated user can read restricted images by pairing a valid
    unrestricted doc_id with a restricted image_id.

    One row per image per document. Images deduplicate by content hash (the hash
    IS the image_id), so the same image appearing in multiple documents yields
    multiple rows here, one per (document_id, image_id) pairing.
    """

    __tablename__ = "document_image"

    document_id: Mapped[UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document.id", ondelete="CASCADE"),
        primary_key=True,
    )
    image_id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )  # SHA256 hex of decoded image bytes
