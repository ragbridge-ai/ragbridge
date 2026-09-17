"""Answer questions from retrieved context.

Behind a ``Protocol``, mirroring ``ragbridge.embeddings.Embedder``, so the
API and tests depend on the shape of a chatter, not on LiteLLM or any
specific provider. Tests and CI use ``FakeChatter`` and never call a real
provider (decision 5).
"""

from typing import Annotated, Protocol

import litellm
from fastapi import Depends

from ragbridge.config import Settings, get_settings

SYSTEM_PROMPT = (
    "You answer questions using only the context provided below. "
    "If the context does not contain the answer, say you don't know - "
    "do not make up information."
)


class Chatter(Protocol):
    async def answer(self, question: str, context: list[str]) -> str:
        """Answer a question, given the text of the retrieved chunks."""
        ...


class LiteLLMChatter:
    """Answers questions through LiteLLM, using the configured chat model."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def answer(self, question: str, context: list[str]) -> str:
        context_text = "\n\n".join(context) if context else "(no relevant documents found)"
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Context:\n{context_text}\n\nQuestion: {question}"},
        ]

        model = self._settings.chat_model
        api_base = self._settings.ollama_base_url if model.startswith("ollama/") else None
        response = await litellm.acompletion(model=model, messages=messages, api_base=api_base)
        content = response.choices[0].message.content
        return content or ""


class FakeChatter:
    """Deterministic chatter for tests: never makes a network call."""

    async def answer(self, question: str, context: list[str]) -> str:
        return f"Fake answer using {len(context)} chunk(s)."


def get_chatter(settings: Annotated[Settings, Depends(get_settings)]) -> Chatter:
    """FastAPI dependency returning the real chatter.

    Tests override this with a ``FakeChatter`` via
    ``app.dependency_overrides``, the same way ``get_embedder`` is
    overridden.
    """
    return LiteLLMChatter(settings)
