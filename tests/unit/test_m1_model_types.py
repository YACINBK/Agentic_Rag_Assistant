"""M1 — model type contract (metadata-only, no DB).

Verifies the D1 timestamp-column fix (all seven timestamp columns are
DateTime(timezone=True)) and the D6 fix (last_login has no server_default,
no onupdate), plus that alembic/env.py pins compare_type=True in both
context.configure() calls.

These tests inspect SQLAlchemy metadata only — no database connection.
"""

from pathlib import Path

import pytest
from sqlalchemy import DateTime

from app.core.models.audit_log import AuditLog
from app.core.models.document import Document
from app.core.models.escalation_event import EscalationEvent
from app.core.models.role import Role
from app.core.models.user import User

# (model, column_name) for every timestamp column across the schema.
_TIMESTAMP_COLUMNS = [
    (Role, "created_at"),
    (User, "created_at"),
    (User, "last_login"),
    (Document, "last_ingested_at"),
    (AuditLog, "created_at"),
    (EscalationEvent, "notified_at"),
    (EscalationEvent, "resolved_at"),
]

# Nullable timestamps that must NOT carry a server_default.
_NULLABLE_NO_DEFAULT = [
    (Document, "last_ingested_at"),
    (EscalationEvent, "resolved_at"),
    (User, "last_login"),
]


@pytest.mark.parametrize("model, column_name", _TIMESTAMP_COLUMNS)
def test_all_timestamp_columns_are_datetime(model, column_name):
    col = model.__table__.c[column_name]
    assert isinstance(col.type, DateTime)
    assert col.type.timezone is True


@pytest.mark.parametrize("model, column_name", _NULLABLE_NO_DEFAULT)
def test_nullable_timestamps_have_no_server_default(model, column_name):
    col = model.__table__.c[column_name]
    assert col.nullable is True
    assert col.server_default is None


def test_last_login_has_no_onupdate():
    col = User.__table__.c["last_login"]
    assert col.onupdate is None


def test_env_pins_compare_type():
    env_text = Path("alembic/env.py").read_text()
    assert env_text.count("compare_type=True") == 2
