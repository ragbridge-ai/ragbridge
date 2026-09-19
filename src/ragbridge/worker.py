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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ragbridge.cache import Cache, RedisCache
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Document
from ragbridge.db.session import create_engine, create_session_factory
from ragbridge.embeddings import Embedder, LiteLLMEmbedder
from ragbridge.ingestion import ingest_document, parse_pages


class JobContext(TypedDict):
    session_factory: async_sessionmaker[AsyncSession]
    embedder: Embedder
    cache: Cache
    settings: Settings
    engine: NotRequired[AsyncEngine]
    """Only set by _on_startup, to dispose of in _on_shutdown - process_document
    itself never needs it, and a test-built JobContext can leave it out."""


async def process_document(ctx: JobContext, document_id: str) -> None:
    """Parse, chunk, and embed one pending document, then mark it ready.

    Any failure - a corrupt PDF, non-UTF-8 text, an embedder error - is
    caught and recorded as ``document.status = "failed"`` with its
    message, rather than re-raised: there is no HTTP response left to
    fail this job onto, since the request that created the document has
    long since returned, and arq's default retry behaviour would
    otherwise retry a permanently broken upload forever.
    """
    session_factory = ctx["session_factory"]
    embedder = ctx["embedder"]
    cache = ctx["cache"]
    settings = ctx["settings"]

    async with session_factory() as session:
        document = await session.get(Document, uuid.UUID(document_id))
        if document is None:
            return

        document.status = "processing"
        await session.commit()

        try:
            raw = document.raw_content
            if raw is None:
                raise ValueError("document has no raw_content to process")
            pages = parse_pages(raw, document.content_type, settings.pdf_extraction)
            await ingest_document(session, document, pages, settings, embedder, cache)
            document.raw_content = None
        except Exception as error:  # broad on purpose: see docstring
            await session.rollback()
            document.status = "failed"
            document.error = str(error)

        await session.commit()


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
