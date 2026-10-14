"""add access_groups to documents and chunks

Revision ID: b84f1c6a2d97
Revises: 5e2b7c1d9a34
Create Date: 2026-10-14 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b84f1c6a2d97"
down_revision: str | Sequence[str] | None = "5e2b7c1d9a34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Every existing document and chunk gets ``{}``, which no scoped caller sees and a
    caller that sends no groups still does, so nothing changes for an existing
    installation. Both columns are the same value, so no chunk needs copying from its
    document (docs/plans/chat-roadmap.md, decision 2).
    """
    for table in ("documents", "chunks"):
        op.add_column(
            table,
            sa.Column(
                "access_groups",
                postgresql.ARRAY(sa.Text()),
                server_default=sa.text("'{}'::text[]"),
                nullable=False,
            ),
        )
        op.create_index(
            f"ix_{table}_access_groups", table, ["access_groups"], postgresql_using="gin"
        )


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("chunks", "documents"):
        op.drop_index(f"ix_{table}_access_groups", table_name=table)
        op.drop_column(table, "access_groups")
