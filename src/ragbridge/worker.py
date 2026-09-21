"""Background worker: processes large document uploads asynchronously.

Runs as a separate process (``uv run arq ragbridge.worker.WorkerSettings``),
not inside the web process. arq is async-native (decision 5,
docs/plans/phase-3.md), so this worker awaits the exact same
``AsyncSession``, ``Embedder``, and ``ingest_document`` the web process
uses, instead of a second, synchronous copy of the ingestion path.
"""

import uuid
from typing import NotRequired, TypedDict

from arq.connections import RedisSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ragbridge.cache import Cache, RedisCache
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Document
from ragbridge.db.session import create_engine, create_session_factory
from ragbridge.embeddings import Embedder, LiteLLMEmbedder
from ragbridge.ingestion import build_chunks, parse_pages, store_chunks


class JobContext(TypedDict):
    session_factory: async_sessionmaker[AsyncSession]
    embedder: Embedder
    cache: Cache
    settings: Settings
    engine: NotRequired[AsyncEngine]
    """Only set by _on_startup, to dispose of in _on_shutdown - process_document
    itself never needs it, and a test-built JobContext can leave it out."""


async def process_document(ctx: JobContext, document_id: str, sha256: str | None = None) -> None:
    """Parse, chunk, and embed one pending document, then mark it ready.

    Any failure - a corrupt PDF, non-UTF-8 text, an embedder error - is
    caught and recorded as ``document.status = "failed"`` with its
    message, rather than re-raised: there is no HTTP response left to
    fail this job onto, since the request that created the document has
    long since returned, and arq's default retry behaviour would
    otherwise retry a permanently broken upload forever.

    ``sha256`` is set for a document a client keeps in sync by external id,
    whose text can be replaced while this job is queued or running. It names
    the content this job was queued for: if the row's hash has moved on, a
    newer write owns the row, and this job does nothing - at the start, before
    the swap, and before recording a failure. It also does nothing when the row
    is already ``"ready"``: a text sent again after another one (X, Y, X) queues
    two jobs for X, and the second must not fail on the raw bytes the first
    already cleared. Only the newest version is ever stored, so a burst of edits
    leaves one. The old chunks stay
    searchable until the swap, and a failure leaves them in place (decision 9,
    docs/plans/external-ids.md). Uploads pass no hash.
    """
    session_factory = ctx["session_factory"]
    embedder = ctx["embedder"]
    cache = ctx["cache"]
    settings = ctx["settings"]

    row_id = uuid.UUID(document_id)
    async with session_factory() as session:
        document = await session.get(Document, row_id)
        if document is None or _obsolete(document, sha256):
            return

        raw = document.raw_content
        content_type = document.content_type
        document.status = "processing"
        await session.commit()

        try:
            if raw is None:
                raise ValueError("document has no raw_content to process")
            pages = parse_pages(raw, content_type, settings.pdf_extraction)
            chunks = await build_chunks(document, pages, settings, embedder)
            # Embedding is done; only now take the row lock, so a write to the
            # same id is never made to wait for it.
            locked = await _lock_current(session, row_id, sha256)
            if locked is None:
                return
            await store_chunks(session, locked, pages, chunks, cache, replace=True)
            locked.raw_content = None
        except Exception as error:  # broad on purpose: see docstring
            await session.rollback()
            locked = await _lock_current(session, row_id, sha256)
            if locked is not None:
                locked.status = "failed"
                locked.error = str(error)

        await session.commit()


def _obsolete(document: Document, sha256: str | None) -> bool:
    """A job queued for content the row no longer holds, or already processed.

    Only for jobs that carry a hash; an upload's job passes none and always runs.
    """
    return sha256 is not None and (document.sha256 != sha256 or document.status == "ready")


async def _lock_current(
    session: AsyncSession, document_id: uuid.UUID, sha256: str | None
) -> Document | None:
    """The document, freshly read and locked - or ``None`` if gone or obsolete."""
    document: Document | None = await session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if document is None or _obsolete(document, sha256):
        return None
    return document


async def _on_startup(ctx: JobContext) -> None:
    """Build this worker process's own engine and embedder.

    A separate process needs its own database connection and embedder
    client, not the web process's - the same reason the web app builds
    its engine in its own lifespan (see ragbridge.main).
    """
    settings = get_settings()
    engine = create_engine(settings)
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    ctx["embedder"] = LiteLLMEmbedder(settings)
    ctx["cache"] = RedisCache(settings.redis_url)
    ctx["settings"] = settings


async def _on_shutdown(ctx: JobContext) -> None:
    await ctx["engine"].dispose()


class WorkerSettings:
    """Read by ``arq ragbridge.worker.WorkerSettings`` on the command line."""

    functions = [process_document]
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    health_check_interval = 30
    """Seconds between the worker's heartbeats, which ``arq --check`` reads.

    arq's default is 3600, and the heartbeat's expiry follows it, so a
    worker that crashed (as opposed to one that shut down cleanly, which
    removes its own heartbeat) would still report healthy for up to an
    hour. 30 seconds makes ``arq --check`` a usable container healthcheck.
    """
