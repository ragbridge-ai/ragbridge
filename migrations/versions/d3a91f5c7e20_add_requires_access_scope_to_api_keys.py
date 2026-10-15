"""add requires_access_scope to api_keys

Revision ID: d3a91f5c7e20
Revises: b84f1c6a2d97
Create Date: 2026-10-15 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3a91f5c7e20"
down_revision: str | Sequence[str] | None = "b84f1c6a2d97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Every existing key gets ``false``: it keeps working exactly as before
    (docs/plans/chat-roadmap.md, decision 3).
    """
    op.add_column(
        "api_keys",
        sa.Column(
            "requires_access_scope",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("api_keys", "requires_access_scope")
