import uuid

from sqlalchemy import Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.core.models.base import Base

EscalationReason = Enum(
    "faithfulness_failure", "relevance_failure",
    name="escalation_reason",
)


class EscalationEvent(Base):
    __tablename__ = "escalation_event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    audit_log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audit_log.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(EscalationReason, nullable=False)
    notified_at: Mapped[str] = mapped_column(server_default=func.now())
    resolved_at: Mapped[str | None] = mapped_column(nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id"), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
