import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.models.base import Base

if TYPE_CHECKING:
    from app.core.models.role import Role

# Who last decided this user's primary role (D40 / M9b).
#   "default"        — nobody has decided; the row carries settings.DEFAULT_ROLE.
#                      M9c's first-login picker keys on exactly this value.
#   "self_selected"  — the user chose it at first login (M9c).
#   "admin_assigned" — an admin set it (M9d).
# Not a privilege tier and not part of the §5 retrieval filter — provenance only.
ROLE_SOURCE_DEFAULT = "default"
ROLE_SOURCE_SELF_SELECTED = "self_selected"
ROLE_SOURCE_ADMIN_ASSIGNED = "admin_assigned"
ROLE_SOURCE_VALUES = (
    ROLE_SOURCE_DEFAULT,
    ROLE_SOURCE_SELF_SELECTED,
    ROLE_SOURCE_ADMIN_ASSIGNED,
)


class User(Base):
    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    keycloak_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("role.id"), nullable=False
    )
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    # server_default backfills every pre-M9b row to "default" at migration time,
    # so no existing user is left NULL on a NOT NULL column. The application still
    # passes the value explicitly when creating a user: after `flush()` a
    # server-default column is unloaded on the Python object, and reading it would
    # give None — which `role_source != "default"` would read as *confirmed*, the
    # exact inversion of what a brand-new user means.
    role_source: Mapped[str] = mapped_column(
        Enum(*ROLE_SOURCE_VALUES, name="role_source"),
        nullable=False,
        server_default=ROLE_SOURCE_DEFAULT,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    role: Mapped["Role"] = relationship(back_populates="users")

    __table_args__ = (
        Index("idx_single_owner", "is_owner", unique=True, postgresql_where=(is_owner.is_(True))),
        CheckConstraint("NOT is_owner OR is_admin", name="ck_owner_implies_admin"),
    )
