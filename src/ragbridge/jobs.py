"""Enqueueing background jobs. See ``ragbridge.worker`` for what runs them.

Behind a ``Protocol``, exactly like ``Embedder``, ``Chatter``, and
``Reranker``: the real implementation needs a running Redis and a
separately-running worker process, neither of which tests or a
key-free ``docker compose up`` (without the ``worker`` service) should
require just to accept an upload.
"""

import uuid
from functools import lru_cache
from typing import Annotated, Protocol

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import Depends

from ragbridge.config import Settings, get_settings
from ragbridge.worker import JobContext, process_document


class JobQueue(Protocol):
    async def enqueue_process_document(self, document_id: uuid.UUID) -> None:
        """Schedule ``document_id`` for background processing."""
        ...


class ArqJobQueue:
    """Enqueues onto a real Redis queue, consumed by ragbridge.worker.

    Connects lazily, on the first call to ``enqueue_process_document``,
    and reuses that connection afterwards - constructing this class,
    like ``LiteLLMEmbedder``, never opens a network connection by
    itself. An installation that never uploads a file over
    ``ASYNC_PROCESSING_THRESHOLD`` never needs Redis running at all.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: ArqRedis | None = None

    async def _connection(self) -> ArqRedis:
        if self._redis is None:
            self._redis = await create_pool(RedisSettings.from_dsn(self._redis_url))
        return self._redis

    async def enqueue_process_document(self, document_id: uuid.UUID) -> None:
        redis = await self._connection()
        await redis.enqueue_job("process_document", str(document_id))


class FakeJobQueue:
    """Runs the job immediately, in the same process - no Redis, no worker.

    Used by tests: it makes ``POST /documents``' asynchronous branch
    observable within a single request/response cycle, the same way
    ``FakeChatter`` makes generation observable without a real LLM.
    """

    def __init__(self, ctx: JobContext) -> None:
        self._ctx = ctx

    async def enqueue_process_document(self, document_id: uuid.UUID) -> None:
        await process_document(self._ctx, str(document_id))


@lru_cache
def _arq_job_queue(redis_url: str) -> ArqJobQueue:
    """One ``ArqJobQueue`` per ``redis_url``, so its lazy connection is
    actually reused across requests instead of reopened on every one -
    the same singleton-via-``lru_cache`` shape as ``get_settings``.
    """
    return ArqJobQueue(redis_url)


def get_job_queue(settings: Annotated[Settings, Depends(get_settings)]) -> JobQueue:
    """FastAPI dependency returning the real job queue.

    Tests override this wholesale with a ``FakeJobQueue`` via
    ``app.dependency_overrides``, the same way ``get_embedder`` is
    overridden with ``FakeEmbedder``.
    """
    return _arq_job_queue(settings.redis_url)
