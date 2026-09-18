"""ORM models for the ragbridge database."""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from ragbridge.config import get_settings
from ragbridge.db.base import Base


class Tenant(Base):
    """A calling application. Documents and API keys belong to one tenant."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    """A hashed API key that authenticates requests for one tenant.

    ``key_hash`` is a SHA-256 digest, not a slow salted hash - see decision
    2 in docs/plans/phase-3.md: the key itself is a 256-bit random secret,
    so a fast hash costs an attacker nothing extra while keeping
    authentication a single indexed lookup. ``prefix`` is stored so a key
    can be identified in a list without ever storing or displaying the
    rest of it. ``revoked_at`` marks a key as no longer valid without
    deleting the row, so what a leaked key touched can still be audited.
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    key_hash: Mapped[str] = mapped_column(unique=True)
    prefix: Mapped[str]
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Document(Base):
    """An uploaded document, before chunking and embedding.

    ``content`` keeps the parsed, joined text, so the document can be
    re-chunked later without asking the user to upload it again. It is
    nullable because a document being processed asynchronously
    (``status`` is ``"pending"`` or ``"processing"``) has no parsed
    content yet - see ``raw_content``. ``sha256`` is unique per tenant,
    not globally - two tenants uploading the same file must get two
    independent documents, or the second tenant would silently receive a
    document it never uploaded (see decision 3, docs/plans/phase-3.md).
    """

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("tenant_id", "sha256"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str]
    content_type: Mapped[str]
    sha256: Mapped[str]
    content: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(default="pending")
    """One of "pending", "processing", "ready", "failed" (ragbridge.api.documents.DocumentStatus).

    Not a database enum or CHECK constraint - kept as a plain column,
    validated at the Pydantic response-model level, the same way
    Settings.retrieval_mode is a Literal enforced by Pydantic and not by
    the database.
    """
    error: Mapped[str | None] = mapped_column(Text, default=None)
    """The failure reason, set only when status is "failed"."""
    raw_content: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    """The original uploaded bytes, kept only until a background worker
    finishes ingesting them (see ragbridge.worker) - unset for a document
    processed synchronously, which never needs its raw bytes again.
    """
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chunk(Base):
    """A chunk of a document's text, with its embedding vector.

    ``chunk_index`` is the chunk's position within its document (0-based),
    used to keep chunks in reading order. ``metadata_`` holds extra facts
    about the chunk's origin, for example the PDF page it came from - the
    Python attribute is not called ``metadata`` because that name is
    already used by SQLAlchemy's ``Base.metadata``. ``tenant_id`` is
    denormalised from its document rather than reached through a join:
    pgvector's HNSW index is approximate, and a query that filters on a
    column outside the index can walk the graph, collect its normal
    quota of nearest neighbours across every tenant, and only then
    discard the ones that fail the filter - under-returning candidates
    for a tenant whose data is a small slice of the table (see decision
    3, docs/plans/phase-3.md).
    """

    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(get_settings().embedding_dimension))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    content_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True)
    )
