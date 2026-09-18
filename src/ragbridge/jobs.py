"""Enqueueing background jobs. See ``ragbridge.worker`` for what runs them.

Behind a ``Protocol``, exactly like ``Embedder``, ``Chatter``, and
``Reranker``: the real implementation needs a running Redis and a
separately-running worker process, neither of which tests or a
key-free ``docker compose up`` (without the ``worker`` service) should
require just to accept an upload.
"""

import uuid
from typing import Protocol

from arq.connections import ArqRedis
from fastapi import Request

from ragbridge.worker import JobContext, process_document


class JobQueue(Protocol):
    async def enqueue_process_document(self, document_id: uuid.UUID) -> None:
        """Schedule ``document_id`` for background processing."""
        ...


class ArqJobQueue:
    """Enqueues onto a real Redis queue, consumed by ragbridge.worker."""

    def __init__(self, redis: ArqRedis) -> None:
        self._redis = redis

    async def enqueue_process_document(self, document_id: uuid.UUID) -> None:
        await self._redis.enqueue_job("process_document", str(document_id))


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


async def get_job_queue(request: Request) -> JobQueue:
    """FastAPI dependency returning the app's job queue.

    Built once, in the app's lifespan, and stored on ``app.state`` - a
    Redis connection pool is not cheap to open fresh on every request,
    unlike ``Embedder``/``Chatter``/``Reranker``'s per-request
    construction. Tests override this wholesale with a ``FakeJobQueue``.
    """
    job_queue: JobQueue = request.app.state.job_queue
    return job_queue
