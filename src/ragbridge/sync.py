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
from ragbridge.jobs import JobQueue

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
    queued: bool = False
    """True when the text was handed to the background worker: the document is
    ``"pending"`` and the request has not embedded it."""


class QueueUnavailable(Exception):
    """The background queue refused the job; the document was marked ``failed``."""


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
    job_queue: JobQueue,
) -> SyncOutcome:
    """Store the record under ``external_id`` and commit.

    Either this request inserts the row, or it takes a row lock on the existing
    one. Every other writer of the same id then waits behind that lock and
    decides against what this request committed (decision 8).

    Text over ``ASYNC_PROCESSING_THRESHOLD`` is not embedded here: the row is
    saved as ``"pending"`` and the worker does the rest (decision 9).
    """
    raw = payload.content.encode()
    in_background = len(raw) > settings.async_processing_threshold
    for _ in range(MAX_ATTEMPTS):
        created = await _insert_if_absent(
            session, tenant_id, external_id, payload, raw if in_background else None
        )
        if created is not None:
            if not in_background:
                await ingest_document(
                    session, created, [payload.content], settings, embedder, cache
                )
            return await _finish(session, created, "created", in_background, job_queue)

        existing = await _lock_existing(session, tenant_id, external_id)
        if existing is not None:
            result, queued = await _apply_to_existing(
                session, existing, payload, raw, in_background, settings, embedder, cache
            )
            return await _finish(session, existing, result, queued, job_queue)
    raise SyncConflict(external_id)


async def _finish(
    session: AsyncSession,
    document: Document,
    result: SyncResult,
    queued: bool,
    job_queue: JobQueue,
) -> SyncOutcome:
    await session.commit()
    await session.refresh(document)
    if queued:
        await _enqueue(session, document, job_queue)
    return SyncOutcome(result, document, queued)


async def _enqueue(session: AsyncSession, document: Document, job_queue: JobQueue) -> None:
    """Hand the document to the worker.

    If the queue is down, the row is marked ``failed``. Left ``"pending"``, it
    would stay that way: a client retrying the same text would be told
    ``unchanged`` and nothing would ever process it. ``failed`` is what makes the
    same text be processed again (decision 6).
    """
    try:
        await job_queue.enqueue_process_document(document.id, document.sha256)
    except Exception as error:
        document.status = "failed"
        document.error = f"could not queue background processing: {error}"
        await session.commit()
        raise QueueUnavailable from error


async def _apply_to_existing(
    session: AsyncSession,
    document: Document,
    payload: SyncPayload,
    raw: bytes,
    in_background: bool,
    settings: Settings,
    embedder: Embedder,
    cache: Cache,
) -> tuple[SyncResult, bool]:
    """Bring a locked, existing document in line with ``payload``.

    Returns the result and whether the text was queued for the worker.
    """
    if _is_older_than_stored(document, payload):
        return "stale", False

    same_text = document.sha256 == content_hash(payload.content)
    if same_text and document.status != "failed":
        return await _update_title_and_metadata(document, payload, cache), False

    # New text, or the same text after a failed attempt (which left no chunks
    # for it): the old chunks are replaced in one transaction, so a search sees
    # the old version or the new one, never a mixture (decision 9).
    title_changed = document.filename != payload.title
    document.filename = payload.title
    document.sha256 = content_hash(payload.content)
    document.metadata_ = payload.metadata
    document.error = None
    _remember_source_time(document, payload)
    if in_background:
        # The old chunks and text stay in place and searchable until the worker
        # swaps them. A new title shows in sources at once, so it must not be
        # served from a cached answer.
        document.raw_content = raw
        document.status = "pending"
        if title_changed:
            await cache.incr(f"corpus_version:{document.tenant_id}")
        return "replaced", True

    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    await ingest_document(session, document, [payload.content], settings, embedder, cache)
    return "replaced", False


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


def _is_older_than_stored(document: Document, payload: SyncPayload) -> bool:
    """An out-of-order write: the record changed in the client before what we hold.

    Equal times are applied, since they are almost always a retry of the same
    write. A write without a time has opted out of ordering, and is applied
    (decision 7). This runs under the row lock, so two writes cannot both pass it
    against the same stored value.
    """
    incoming, stored = payload.source_updated_at, document.source_updated_at
    return incoming is not None and stored is not None and incoming < stored


def _remember_source_time(document: Document, payload: SyncPayload) -> None:
    if payload.source_updated_at is not None:
        document.source_updated_at = payload.source_updated_at


async def _lock_existing(
    session: AsyncSession, tenant_id: uuid.UUID, external_id: str
) -> Document | None:
    """The row for ``external_id``, locked until this transaction ends.

    Nothing else would make a competing writer wait until this one has sent its
    first ``UPDATE``; the lock closes that window, and the ordering check of
    ``_is_older_than_stored`` depends on it.
    """
    document: Document | None = await session.scalar(
        select(Document)
        .where(Document.tenant_id == tenant_id, Document.external_id == external_id)
        .with_for_update()
    )
    return document


async def _insert_if_absent(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    external_id: str,
    payload: SyncPayload,
    raw_content: bytes | None,
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
            raw_content=raw_content,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "external_id"])
        .returning(Document)
    )
    result = await session.scalars(statement)
    document: Document | None = result.one_or_none()
    return document
