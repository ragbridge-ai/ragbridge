"""Unit tests for the fake embedder and CachingEmbedder. No network calls,
no database needed - CachingEmbedder is tested against a FakeCache and a
call-counting fake, never a real Redis."""

import asyncio

from ragbridge.cache import FakeCache
from ragbridge.embeddings import CachingEmbedder, FakeEmbedder


class _CountingEmbedder:
    """Wraps FakeEmbedder, counting how many texts it was actually asked
    to embed - what proves a cache hit skipped the wrapped embedder,
    not just that the two calls happened to return the same vector.
    """

    def __init__(self) -> None:
        self._inner = FakeEmbedder(dimension=8)
        self.calls: list[str] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.extend(texts)
        return await self._inner.embed(texts)


def test_fake_embedder_returns_one_vector_per_text() -> None:
    embedder = FakeEmbedder(dimension=8)

    result = asyncio.run(embedder.embed(["hello", "world"]))

    assert len(result) == 2
    assert all(len(vector) == 8 for vector in result)


def test_fake_embedder_is_deterministic_per_text() -> None:
    embedder = FakeEmbedder(dimension=8)

    first = asyncio.run(embedder.embed(["hello, ragbridge"]))
    second = asyncio.run(embedder.embed(["hello, ragbridge"]))

    assert first == second


def test_fake_embedder_gives_different_texts_different_vectors() -> None:
    embedder = FakeEmbedder(dimension=8)

    result = asyncio.run(embedder.embed(["hello", "goodbye"]))

    assert result[0] != result[1]


def test_caching_embedder_embeds_the_same_text_only_once() -> None:
    inner = _CountingEmbedder()
    cache = CachingEmbedder(inner, FakeCache(), model="test-model", ttl=60)

    first = asyncio.run(cache.embed(["hello, ragbridge"]))
    second = asyncio.run(cache.embed(["hello, ragbridge"]))

    assert first == second
    assert inner.calls == ["hello, ragbridge"]


def test_caching_embedder_still_embeds_different_texts() -> None:
    inner = _CountingEmbedder()
    cache = CachingEmbedder(inner, FakeCache(), model="test-model", ttl=60)

    asyncio.run(cache.embed(["hello"]))
    asyncio.run(cache.embed(["goodbye"]))

    assert inner.calls == ["hello", "goodbye"]


def test_caching_embedder_keys_by_model_so_a_model_change_is_never_a_stale_hit() -> None:
    inner = _CountingEmbedder()
    shared_cache = FakeCache()
    embedder_a = CachingEmbedder(inner, shared_cache, model="model-a", ttl=60)
    embedder_b = CachingEmbedder(inner, shared_cache, model="model-b", ttl=60)

    asyncio.run(embedder_a.embed(["same text"]))
    asyncio.run(embedder_b.embed(["same text"]))

    assert inner.calls == ["same text", "same text"]


def test_caching_embedder_handles_a_mix_of_cached_and_uncached_texts() -> None:
    inner = _CountingEmbedder()
    cache = CachingEmbedder(inner, FakeCache(), model="test-model", ttl=60)
    asyncio.run(cache.embed(["cached"]))
    inner.calls.clear()

    result = asyncio.run(cache.embed(["cached", "new"]))

    assert inner.calls == ["new"]
    assert len(result) == 2


def test_caching_embedder_never_touches_the_cache_for_an_empty_list() -> None:
    inner = _CountingEmbedder()
    cache = CachingEmbedder(inner, FakeCache(), model="test-model", ttl=60)

    result = asyncio.run(cache.embed([]))

    assert result == []
    assert inner.calls == []
