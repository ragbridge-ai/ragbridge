"""ORM models for the ragbridge database."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

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
