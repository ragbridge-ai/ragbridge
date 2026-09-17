"""Turn text into embedding vectors.

Behind a ``Protocol`` so the API and tests depend on the shape of an
embedder, not on LiteLLM or any specific provider. Tests and CI use
``FakeEmbedder`` and never call a real provider (decision 5): no API key
or running Ollama instance is needed to run the test suite.
"""

import random
from typing import Annotated, Protocol

import litellm
from fastapi import Depends

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


def get_embedder(settings: Annotated[Settings, Depends(get_settings)]) -> Embedder:
    """FastAPI dependency returning the real embedder.

    Tests override this with a ``FakeEmbedder`` via
    ``app.dependency_overrides``, the same way ``app_with_database``
    overrides the database engine.
    """
    return LiteLLMEmbedder(settings)
