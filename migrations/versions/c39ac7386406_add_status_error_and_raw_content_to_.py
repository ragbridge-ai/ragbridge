"""add status, error, and raw_content to documents

Revision ID: c39ac7386406
Revises: 3cb3c5c7dcce
Create Date: 2026-09-18 13:27:24.022182

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c39ac7386406"
down_revision: str | Sequence[str] | None = "3cb3c5c7dcce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default only applies to existing rows: every document that
    # predates status tracking already has its content and chunks, so
    # "ready" is the correct backfill value, not just a placeholder. New
    # rows get their status from the ORM's Python-side default instead
    # (Document.status in db/models.py), which always supplies a value
    # explicitly and so never falls back to this one.
    op.add_column(
        "documents",
        sa.Column("status", sa.String(), nullable=False, server_default="ready"),
    )
    op.add_column("documents", sa.Column("error", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("raw_content", sa.LargeBinary(), nullable=True))
    op.alter_column("documents", "content", existing_type=sa.TEXT(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column("documents", "content", existing_type=sa.TEXT(), nullable=False)
    op.drop_column("documents", "raw_content")
    op.drop_column("documents", "error")
    op.drop_column("documents", "status")
