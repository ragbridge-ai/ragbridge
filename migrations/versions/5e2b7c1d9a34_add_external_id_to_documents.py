"""add external_id, metadata and timestamps to documents

Revision ID: 5e2b7c1d9a34
Revises: c39ac7386406
Create Date: 2026-09-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "5e2b7c1d9a34"
down_revision: str | Sequence[str] | None = "c39ac7386406"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    No existing row has an ``external_id``, so the partial index below accepts
    exactly the rows the old ``UNIQUE (tenant_id, sha256)`` did and nothing is
    rewritten (docs/adr/0010-external-document-ids.md).
    """
    op.add_column("documents", sa.Column("external_id", sa.String(length=255), nullable=True))
    op.add_column(
        "documents",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "documents", sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "documents",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.drop_constraint(op.f("uq_documents_tenant_id"), "documents", type_="unique")
    op.create_index(
        "uq_documents_tenant_id_sha256_uploads",
        "documents",
        ["tenant_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("external_id IS NULL"),
    )
    op.create_unique_constraint(
        "uq_documents_tenant_id_external_id", "documents", ["tenant_id", "external_id"]
    )


def downgrade() -> None:
    """Downgrade schema.

    Refuses while any document has an ``external_id``: two synced documents may
    hold identical text, and ``UNIQUE (tenant_id, sha256)`` cannot be restored
    over them.
    """
    synced = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM documents WHERE external_id IS NOT NULL")
    )
    if synced:
        raise RuntimeError(
            f"Cannot downgrade: {synced} document(s) have an external_id and may share "
            "content. Delete them first (DELETE /documents/external/{external_id})."
        )
    op.drop_constraint("uq_documents_tenant_id_external_id", "documents", type_="unique")
    op.drop_index("uq_documents_tenant_id_sha256_uploads", table_name="documents")
    op.create_unique_constraint(
        op.f("uq_documents_tenant_id"), "documents", ["tenant_id", "sha256"]
    )
    op.drop_column("documents", "updated_at")
    op.drop_column("documents", "source_updated_at")
    op.drop_column("documents", "metadata")
    op.drop_column("documents", "external_id")
