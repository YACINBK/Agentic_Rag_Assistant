import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.sql import func

from app.core.models.base import Base

if TYPE_CHECKING:
    from app.core.models.user import User

# Reserved names — never valid as a Role.name (CLAUDE.md §2, §10):
#   "all"   is the allowed_roles wildcard sentinel
#   "admin" and "owner" are flags, not roles
# A Role row with any of these would silently corrupt the §5 retrieval filter.
_RESERVED_ROLE_NAMES = {"all", "admin", "owner"}


class Role(Base):
    __tablename__ = "role"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    persona_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    users: Mapped[list["User"]] = relationship(back_populates="role")

    @validates("name")
    def _reject_reserved_names(self, key: str, value: str) -> str:
        if value is not None and value.strip().lower() in _RESERVED_ROLE_NAMES:
            raise ValueError(
                f"Role name {value!r} is reserved and cannot be used "
                f"(reserved: {', '.join(sorted(_RESERVED_ROLE_NAMES))})"
            )
        return value
