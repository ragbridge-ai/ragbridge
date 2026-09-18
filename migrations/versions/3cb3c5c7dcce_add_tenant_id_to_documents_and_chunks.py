"""add tenant_id to documents and chunks

Revision ID: 3cb3c5c7dcce
Revises: 29ce7af6013e
Create Date: 2026-09-18 13:10:09.446591

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3cb3c5c7dcce"
down_revision: str | Sequence[str] | None = "29ce7af6013e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("chunks", sa.Column("tenant_id", sa.Uuid(), nullable=False))
    op.create_index(op.f("ix_chunks_tenant_id"), "chunks", ["tenant_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_chunks_tenant_id_tenants"),
        "chunks",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column("documents", sa.Column("tenant_id", sa.Uuid(), nullable=False))
    op.drop_constraint(op.f("uq_documents_sha256"), "documents", type_="unique")
    op.create_index(op.f("ix_documents_tenant_id"), "documents", ["tenant_id"], unique=False)
    op.create_unique_constraint(
        op.f("uq_documents_tenant_id"), "documents", ["tenant_id", "sha256"]
    )
    op.create_foreign_key(
        op.f("fk_documents_tenant_id_tenants"),
        "documents",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("fk_documents_tenant_id_tenants"), "documents", type_="foreignkey")
    op.drop_constraint(op.f("uq_documents_tenant_id"), "documents", type_="unique")
    op.drop_index(op.f("ix_documents_tenant_id"), table_name="documents")
    op.create_unique_constraint(op.f("uq_documents_sha256"), "documents", ["sha256"])
    op.drop_column("documents", "tenant_id")
    op.drop_constraint(op.f("fk_chunks_tenant_id_tenants"), "chunks", type_="foreignkey")
    op.drop_index(op.f("ix_chunks_tenant_id"), table_name="chunks")
    op.drop_column("chunks", "tenant_id")
