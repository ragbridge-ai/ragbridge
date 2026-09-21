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

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.cache import Cache
from ragbridge.config import Settings
from ragbridge.db.models import Chunk, Document
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


class SyncConflict(Exception):
    """The id kept appearing and disappearing under this request; the client should retry."""


MAX_ATTEMPTS = 3
"""Tries at insert-or-lock. One is nearly always enough; a second is needed only
when another request deletes the row between our failed insert and our lock."""


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
    """Store the record under ``external_id`` and commit.

    Either this request inserts the row, or it takes a row lock on the existing
    one. Every other writer of the same id then waits behind that lock and
    decides against what this request committed (decision 8).
    """
    for _ in range(MAX_ATTEMPTS):
        created = await _insert_if_absent(session, tenant_id, external_id, payload)
        if created is not None:
            await ingest_document(session, created, [payload.content], settings, embedder, cache)
            await session.commit()
            await session.refresh(created)
            return SyncOutcome("created", created)

        existing = await _lock_existing(session, tenant_id, external_id)
        if existing is not None:
            result = await _apply_to_existing(session, existing, payload, settings, embedder, cache)
            await session.commit()
            await session.refresh(existing)
            return SyncOutcome(result, existing)
    raise SyncConflict(external_id)


async def _apply_to_existing(
    session: AsyncSession,
    document: Document,
    payload: SyncPayload,
    settings: Settings,
    embedder: Embedder,
    cache: Cache,
) -> SyncResult:
    """Bring a locked, existing document in line with ``payload``."""
    same_text = document.sha256 == content_hash(payload.content)
    if same_text and document.status != "failed":
        return await _update_title_and_metadata(document, payload, cache)

    # New text, or the same text after a failed attempt (which left no chunks):
    # swap the chunks. Deleting and re-adding happen in one transaction, so a
    # search sees the old version or the new one, never a mixture (decision 9).
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    document.filename = payload.title
    document.sha256 = content_hash(payload.content)
    document.metadata_ = payload.metadata
    document.error = None
    _remember_source_time(document, payload)
    await ingest_document(session, document, [payload.content], settings, embedder, cache)
    return "replaced"


async def _update_title_and_metadata(
    document: Document, payload: SyncPayload, cache: Cache
) -> SyncResult:
    """The text is unchanged: no re-chunking and no re-embedding (decision 6).

    A new title changes what a cached answer shows in its sources, so it
    invalidates the tenant's answer cache. Metadata is not part of any answer.
    """
    title_changed = document.filename != payload.title
    metadata_changed = document.metadata_ != payload.metadata
    if title_changed:
        document.filename = payload.title
    if metadata_changed:
        document.metadata_ = payload.metadata
    _remember_source_time(document, payload)
    if title_changed:
        await cache.incr(f"corpus_version:{document.tenant_id}")
    return "updated" if title_changed or metadata_changed else "unchanged"


def _remember_source_time(document: Document, payload: SyncPayload) -> None:
    if payload.source_updated_at is not None:
        document.source_updated_at = payload.source_updated_at


async def _lock_existing(
    session: AsyncSession, tenant_id: uuid.UUID, external_id: str
) -> Document | None:
    """The row for ``external_id``, locked until this transaction ends."""
    document: Document | None = await session.scalar(
        select(Document)
        .where(Document.tenant_id == tenant_id, Document.external_id == external_id)
        .with_for_update()
    )
    return document


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
