"""A small cache, backed by Redis. Used for embeddings (always) and
whole answers (opt-in) - see decision 7, docs/plans/phase-3.md.

Behind a ``Protocol``, exactly like ``Embedder``, ``Chatter``,
``Reranker``, and ``JobQueue``: tests never need a real Redis.
"""

from functools import lru_cache
from typing import Annotated, Protocol

from fastapi import Depends
from redis.asyncio import Redis

from ragbridge.config import Settings, get_settings


class Cache(Protocol):
    async def get(self, key: str) -> str | None:
        """Return the cached value for ``key``, or None if missing or expired."""
        ...

    async def set(self, key: str, value: str, *, ttl: int) -> None:
        """Store ``value`` under ``key``. ``ttl` <= 0`` means no expiry."""
        ...

    async def incr(self, key: str) -> int:
        """Atomically increment ``key`` (starting from 0) and return the new value."""
        ...


class RedisCache:
    """Connects lazily, on first use, like ``ArqJobQueue`` - constructing
    this class never opens a network connection, so an installation that
    never enables the answer cache and never uploads a document large
    enough to hit the embedding cache never needs Redis running.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: Redis | None = None

    def _connection(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    async def get(self, key: str) -> str | None:
        value = await self._connection().get(key)
        return value if value is None else str(value)

    async def set(self, key: str, value: str, *, ttl: int) -> None:
        await self._connection().set(key, value, ex=ttl if ttl > 0 else None)

    async def incr(self, key: str) -> int:
        return int(await self._connection().incr(key))


class FakeCache:
    """An in-memory cache for tests: no Redis, and no TTL enforcement -
    what these tests exercise is caching *logic* (a set value is visible
    to a later get, incr behaves like Redis's INCR), not eviction timing.
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._values.get(key)

    async def set(self, key: str, value: str, *, ttl: int) -> None:
        self._values[key] = value

    async def incr(self, key: str) -> int:
        value = int(self._values.get(key, "0")) + 1
        self._values[key] = str(value)
        return value


@lru_cache
def _redis_cache(redis_url: str) -> RedisCache:
    """One ``RedisCache`` per ``redis_url``, so its lazy connection is
    reused across requests - the same shape as ``jobs._arq_job_queue``.
    """
    return RedisCache(redis_url)


def get_cache(settings: Annotated[Settings, Depends(get_settings)]) -> Cache:
    """FastAPI dependency returning the real cache.

    Tests override this wholesale with a ``FakeCache``.
    """
    return _redis_cache(settings.redis_url)
