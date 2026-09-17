"""ORM models for the ragbridge database."""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from ragbridge.config import get_settings
from ragbridge.db.base import Base


class Document(Base):
    """An uploaded document, before chunking and embedding.

    ``content`` keeps the raw uploaded text, so the document can be
    re-chunked later (once chunking exists) without asking the user to
    upload it again.
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    filename: Mapped[str]
    content_type: Mapped[str]
    sha256: Mapped[str] = mapped_column(unique=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Chunk(Base):
    """A chunk of a document's text, with its embedding vector.

    ``chunk_index`` is the chunk's position within its document (0-based),
    used to keep chunks in reading order. ``metadata_`` holds extra facts
    about the chunk's origin, for example the PDF page it came from - the
    Python attribute is not called ``metadata`` because that name is
    already used by SQLAlchemy's ``Base.metadata``.
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
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    chunk_index: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(get_settings().embedding_dimension))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
