"""Turn text into embedding vectors.

Behind a ``Protocol`` so the API and tests depend on the shape of an
embedder, not on LiteLLM or any specific provider. Tests and CI use
``FakeEmbedder`` and never call a real provider (decision 5): no API key
or running Ollama instance is needed to run the test suite.
"""

import hashlib
import json
import random
from typing import Annotated, Protocol

import litellm
from fastapi import Depends

from ragbridge.cache import Cache, get_cache
from ragbridge.config import Settings, get_settings


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text, in the same order."""
        ...


class LiteLLMEmbedder:
    """Embeds text through LiteLLM, using the configured model."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._settings.embedding_model
        api_base = self._settings.ollama_base_url if model.startswith("ollama/") else None
        response = await litellm.aembedding(model=model, input=texts, api_base=api_base)
        return [item["embedding"] for item in response.data]


class FakeEmbedder:
    """Deterministic embedder for tests: never makes a network call.

    The same text always produces the same vector (seeded by the text
    itself), which is enough to test that chunks and their embeddings
    are stored and wired up correctly, without needing real semantic
    similarity.
    """

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        rng = random.Random(text)
        return [rng.uniform(-1.0, 1.0) for _ in range(self._dimension)]


class CachingEmbedder:
    """Wraps another ``Embedder``, caching each text's vector by (model, text).

    An embedding is a pure function of ``(model, text)``: putting the
    model name in the cache key makes a stale hit impossible, which is
    what makes this safe to enable by default (decision 7,
    docs/plans/phase-3.md) - unlike the answer cache, this one never
    needs to be invalidated.
    """

    def __init__(self, embedder: Embedder, cache: Cache, *, model: str, ttl: int) -> None:
        self._embedder = embedder
        self._cache = cache
        self._model = model
        self._ttl = ttl

    def _key(self, text: str) -> str:
        digest = hashlib.sha256(text.encode()).hexdigest()
        return f"embedding:{self._model}:{digest}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        keys = [self._key(text) for text in texts]
        values: list[str | None] = [await self._cache.get(key) for key in keys]

        misses = [index for index, value in enumerate(values) if value is None]
        if misses:
            fresh = await self._embedder.embed([texts[index] for index in misses])
            for index, vector in zip(misses, fresh, strict=True):
                serialized = json.dumps(vector)
                values[index] = serialized
                await self._cache.set(keys[index], serialized, ttl=self._ttl)

        result: list[list[float]] = []
        for value in values:
            assert value is not None  # every miss was just filled in above
            result.append(json.loads(value))
        return result


def get_embedder(
    settings: Annotated[Settings, Depends(get_settings)],
    cache: Annotated[Cache, Depends(get_cache)],
) -> Embedder:
    """FastAPI dependency returning the real embedder, cached unless disabled.

    Tests override this with a ``FakeEmbedder`` via
    ``app.dependency_overrides``, the same way ``app_with_database``
    overrides the database engine - bypassing ``CachingEmbedder``
    entirely, since a deterministic fake needs no caching to test.
    """
    base = LiteLLMEmbedder(settings)
    if settings.embedding_cache_ttl <= 0:
        return base
    return CachingEmbedder(
        base, cache, model=settings.embedding_model, ttl=settings.embedding_cache_ttl
    )
