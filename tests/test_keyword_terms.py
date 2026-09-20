"""Tests for leaving out, from a keyword query, words that occur in most chunks.

A word found in nearly every chunk (a product name, "project") says nothing about which
chunk answers: it only lets every generic chunk match. In a module of its own so it
cannot conflict with changes to ``test_retrieval.py``.
"""

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.auth import hash_api_key
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import ApiKey
from ragbridge.retrieval import keyword_search

TARGET = (
    "Web framework: the service is built on FastAPI, a modern web framework that is quick to "
    "learn and easy to test in small steps."
)


def _generic(index: int, word: str = "Zenvora") -> str:
    return (
        f"{word} community page number {index}: the {word} project keeps this page simple, and "
        f"every reader of the {word} project is welcome to ask the {word} team about page {index}."
    )


def _corpus(generic: int = 24, marked: int | None = None) -> str:
    """Generic paragraphs plus the one target chunk. ``marked`` limits how many say "Zenvora"."""
    paragraphs = [
        _generic(i, "Zenvora" if marked is None or i < marked else "Orbital")
        for i in range(generic)
    ]
    return "\n\n".join([*paragraphs, TARGET]) + "\n"


def _upload(app: FastAPI, key: str, text: str) -> None:
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    assert client.post("/documents", files={"file": ("d.md", text.encode(), "text/markdown")})


def _keyword(app: FastAPI, key: str, query: str, share: float) -> list[str]:
    async def run() -> list[str]:
        session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
        async with session_factory() as session:
            api_key = await session.scalar(
                select(ApiKey).where(ApiKey.key_hash == hash_api_key(key))
            )
            assert api_key is not None
            tenant_id: uuid.UUID = api_key.tenant_id
            rows = await keyword_search(
                session, query, limit=40, tenant_id=tenant_id, max_term_share=share
            )
        return [chunk.content for chunk, _, _ in rows]

    return asyncio.run(run())


def test_a_word_in_most_chunks_is_left_out_so_only_the_distinctive_words_match(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, _corpus())

    assert _keyword(app_with_database, tenant_with_key, "Zenvora web framework", 0.5) == [TARGET]


def test_without_the_filter_the_common_word_lets_every_generic_chunk_match(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """The problem: 'zenvora' is in no target, so the strict query matched nothing and
    the OR fallback credited all 24 generic chunks.
    """
    _upload(app_with_database, tenant_with_key, _corpus())

    found = _keyword(app_with_database, tenant_with_key, "Zenvora web framework", 1.0)

    assert TARGET in found and len(found) > 20


def test_the_or_fallback_does_not_credit_chunks_that_only_share_the_common_word(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, _corpus())
    question = "Which web framework does the Zenvora project use, and what is deployed on Mars?"

    assert _keyword(app_with_database, tenant_with_key, question, 0.5) == [TARGET]
    assert len(_keyword(app_with_database, tenant_with_key, question, 1.0)) > 20


def test_a_word_in_fewer_chunks_than_the_share_is_kept_and_one_above_it_is_dropped(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """'Zenvora' is in 8 of 25 chunks (32%), 'Orbital' in 16 (64%): with a 50% share the
    first still counts and the second is left out.
    """
    _upload(app_with_database, tenant_with_key, _corpus(generic=24, marked=8))

    found = _keyword(app_with_database, tenant_with_key, "Orbital Zenvora web framework", 0.5)

    assert TARGET in found
    assert sum("Zenvora" in chunk for chunk in found) == 8
    assert sum("Orbital" in chunk for chunk in found) == 0


def test_a_small_corpus_is_left_alone(app_with_database: FastAPI, tenant_with_key: str) -> None:
    """With five chunks every word is 'common': frequency means nothing yet."""
    _upload(app_with_database, tenant_with_key, _corpus(generic=4))

    filtered = _keyword(app_with_database, tenant_with_key, "Zenvora web framework", 0.5)
    unfiltered = _keyword(app_with_database, tenant_with_key, "Zenvora web framework", 1.0)

    assert filtered == unfiltered and len(filtered) == 5


def test_a_query_made_only_of_common_words_is_kept_as_typed(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, _corpus())

    found = _keyword(app_with_database, tenant_with_key, "Zenvora project", 0.5)

    assert len(found) == 24


@pytest.mark.parametrize("query", ['"Zenvora project" web', "web framework -Zenvora"])
def test_quoted_phrases_and_exclusions_are_never_filtered(
    app_with_database: FastAPI, tenant_with_key: str, query: str
) -> None:
    _upload(app_with_database, tenant_with_key, _corpus())

    assert _keyword(app_with_database, tenant_with_key, query, 0.5) == _keyword(
        app_with_database, tenant_with_key, query, 1.0
    )


def test_a_typed_or_survives_the_filter(app_with_database: FastAPI, tenant_with_key: str) -> None:
    _upload(app_with_database, tenant_with_key, _corpus())

    found = _keyword(app_with_database, tenant_with_key, "Zenvora framework or database", 0.5)

    assert TARGET in found


def test_search_gives_keyword_credit_only_to_the_chunk_with_the_distinctive_words(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        keyword_max_term_frequency=0.5
    )
    _upload(app_with_database, tenant_with_key, _corpus())
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    hits = client.post(
        "/search", json={"query": "Zenvora web framework", "top_k": 10, "explain": True}
    ).json()["results"]

    with_keyword = [h for h in hits if h["retrieval"]["keyword_rank"] is not None]
    assert [h["content"] for h in with_keyword] == [TARGET]
    assert with_keyword[0]["retrieval"]["keyword_rank"] == 1


def test_the_setting_can_turn_the_filter_off(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        keyword_max_term_frequency=1.0
    )
    _upload(app_with_database, tenant_with_key, _corpus())
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    hits = client.post(
        "/search", json={"query": "Zenvora web framework", "top_k": 10, "explain": True}
    ).json()["results"]

    assert sum(h["retrieval"]["keyword_rank"] is not None for h in hits) > 5


def test_the_filter_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYWORD_MAX_TERM_FREQUENCY", raising=False)

    assert Settings().keyword_max_term_frequency == 0.5
