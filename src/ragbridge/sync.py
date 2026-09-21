"""Create, replace or update a document that a client application keeps in sync.

The client names its record with an ``external_id`` and sends its current state
in one call, so the call is safe to repeat: see ``docs/plans/external-ids.md``.
The HTTP layer (``ragbridge.api.documents``) turns a ``SyncOutcome`` into a
response; everything that touches the database lives here.

Two writers for one id are serialised by the database, not by this code. The
unique constraint ``(tenant_id, external_id)`` makes a second row impossible,
whatever the code does; ``INSERT ... ON CONFLICT DO NOTHING`` then turns the
losing insert into "the row already exists" instead of an error (decision 8).
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.cache import Cache
from ragbridge.config import Settings
from ragbridge.db.models import Document
from ragbridge.embeddings import Embedder
from ragbridge.ingestion import ingest_document

SyncResult = Literal["created", "replaced", "updated", "unchanged", "stale"]


@dataclass(frozen=True)
class SyncPayload:
    """What the client sent for one record, already validated."""

    title: str
    content: str
    metadata: dict[str, Any]
    source_updated_at: datetime | None


@dataclass(frozen=True)
class SyncOutcome:
    """What happened, and the document as it now stands."""

    result: SyncResult
    document: Document


class ExternalIdTaken(Exception):
    """The id already has a document. Removed when replacing is implemented."""


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


async def put_external_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    external_id: str,
    payload: SyncPayload,
    settings: Settings,
    embedder: Embedder,
    cache: Cache,
) -> SyncOutcome:
    """Store the record under ``external_id`` and commit."""
    document = await _insert_if_absent(session, tenant_id, external_id, payload)
    if document is None:
        raise ExternalIdTaken(external_id)

    await ingest_document(session, document, [payload.content], settings, embedder, cache)
    await session.commit()
    await session.refresh(document)
    return SyncOutcome("created", document)


async def _insert_if_absent(
    session: AsyncSession, tenant_id: uuid.UUID, external_id: str, payload: SyncPayload
) -> Document | None:
    """Insert the row, or return ``None`` if the id already has one.

    Where another transaction is inserting the same id and has not committed,
    the database waits for it, then reports the conflict. The new row is a
    ``"pending"`` shell that ingestion completes in the same transaction, so no
    other request ever sees it half-built.
    """
    statement = (
        insert(Document)
        .values(
            tenant_id=tenant_id,
            external_id=external_id,
            filename=payload.title,
            content_type="text/plain",
            sha256=content_hash(payload.content),
            metadata_=payload.metadata,
            source_updated_at=payload.source_updated_at,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "external_id"])
        .returning(Document)
    )
    result = await session.scalars(statement)
    document: Document | None = result.one_or_none()
    return document
