"""add user.role_source

Records who last decided a user's primary role (D40 / M9b). PostgreSQL is
authoritative for the role itself; this column carries only the provenance that
M9c's first-login picker and M9d's admin list read.

Revision ID: 80ee99c0433a
Revises: ddb267266304
Create Date: 2026-08-26 16:20:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "80ee99c0433a"
down_revision: Union[str, None] = "ddb267266304"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Spelled out here rather than imported from app.core.models.user: a migration must
# describe the schema as it was at this revision, so importing the model would make
# this file change meaning whenever the model changes. Same reasoning as
# `_role_table()` in the initial revision.
ROLE_SOURCE_TYPE_NAME = "role_source"
ROLE_SOURCE_VALUES = ("default", "self_selected", "admin_assigned")
ROLE_SOURCE_DEFAULT = "default"


def upgrade() -> None:
    bind = op.get_bind()

    # Create the native type as its own statement. `op.add_column()` with an inline
    # enum does not reliably emit CREATE TYPE first on PostgreSQL — it fails with
    # `type "role_source" does not exist`. checkfirst=True so a half-applied run
    # can be repeated.
    sa.Enum(*ROLE_SOURCE_VALUES, name=ROLE_SOURCE_TYPE_NAME).create(bind, checkfirst=True)

    # server_default is what backfills the existing rows (assertion 10): adding a
    # NOT NULL column to a populated table without one fails outright. It stays on
    # the column afterwards rather than being dropped, so a plain INSERT that omits
    # role_source is still valid.
    #
    # create_type=False — the type exists as of the statement above; leaving the
    # default would make SQLAlchemy emit a second CREATE TYPE and raise
    # DuplicateObject.
    op.add_column(
        "user",
        sa.Column(
            "role_source",
            postgresql.ENUM(
                *ROLE_SOURCE_VALUES,
                name=ROLE_SOURCE_TYPE_NAME,
                create_type=False,
            ),
            nullable=False,
            server_default=ROLE_SOURCE_DEFAULT,
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "role_source")

    # A native enum type outlives the column that used it. Alembic emits no
    # DROP TYPE, so without this the next `upgrade head` fails with
    # `type "role_source" already exists` — the same hazard documented at
    # alembic/versions/ddb267266304_initial_schema.py:238-243. That file's
    # ENUM_TYPE_NAMES tuple is deliberately left alone: this revision drops the
    # type it created, and adding it there would make the initial downgrade fail
    # on a database that never reached this revision.
    bind = op.get_bind()
    sa.Enum(name=ROLE_SOURCE_TYPE_NAME).drop(bind, checkfirst=False)
