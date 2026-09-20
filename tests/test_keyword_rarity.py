"""Tests for ranking the keyword OR fallback by how rare each matched word is.

PostgreSQL's ``ts_rank`` gives every word the same weight, so a chunk matching three
common words (api, documentation, page) outranks the one chunk that contains the single
rare word the question is really about. In a module of its own so it cannot conflict with
changes to ``test_retrieval.py``.
"""

import asyncio

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
    "Explorer tool: the quibble viewer opens the reference in a browser window, and the quibble "
    "tab shows each request and response for a running service."
)
QUESTION = "How can I open the quibble API documentation page?"


def _overview(index: int) -> str:
    return (
        f"Overview of the API documentation page {index}: this page lists every API reference "
        f"and documentation entry for readers, page {index}, with notes on each API call."
    )


def _filler(index: int) -> str:
    return (
        f"Community note {index}: volunteers share news, photos and stories about local events "
        f"every week, so neighbours can read about event number {index} and join in."
    )


def _corpus(overview: int = 12, filler: int = 17) -> str:
    paragraphs = [_overview(i) for i in range(overview)] + [_filler(i) for i in range(filler)]
    return "\n\n".join([*paragraphs, TARGET]) + "\n"


def _upload(app: FastAPI, key: str, text: str) -> None:
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    assert client.post("/documents", files={"file": ("d.md", text.encode(), "text/markdown")})


def _keyword(app: FastAPI, key: str, query: str, *, weighting: bool) -> list[tuple[str, float]]:
    async def run() -> list[tuple[str, float]]:
        session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
        async with session_factory() as session:
            api_key = await session.scalar(
                select(ApiKey).where(ApiKey.key_hash == hash_api_key(key))
            )
            assert api_key is not None
            rows = await keyword_search(
                session, query, limit=40, tenant_id=api_key.tenant_id, rarity_weighting=weighting
            )
        return [(chunk.content, score) for chunk, _, score in rows]

    return asyncio.run(run())


def test_the_one_chunk_with_the_rare_word_comes_first_when_weighted_by_rarity(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, _corpus())

    found = _keyword(app_with_database, tenant_with_key, QUESTION, weighting=True)

    assert found[0][0] == TARGET


def test_without_the_weighting_chunks_with_several_common_words_outrank_it(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """The reported problem, with the setting off."""
    _upload(app_with_database, tenant_with_key, _corpus())

    found = _keyword(app_with_database, tenant_with_key, QUESTION, weighting=False)

    assert TARGET in [content for content, _ in found]
    assert found[0][0] != TARGET


def test_the_score_is_the_sum_of_the_rarity_weights_and_is_ordered_best_first(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, _corpus())

    scores = [
        score for _, score in _keyword(app_with_database, tenant_with_key, QUESTION, weighting=True)
    ]

    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[1] > 0


def test_a_chunk_matching_only_common_words_ranks_below_one_with_a_rare_word(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _upload(app_with_database, tenant_with_key, _corpus())

    contents = [
        c for c, _ in _keyword(app_with_database, tenant_with_key, QUESTION, weighting=True)
    ]

    assert contents.index(TARGET) < min(
        i for i, content in enumerate(contents) if content.startswith("Overview")
    )


def test_a_query_that_matches_strictly_is_ranked_as_before(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """The weighting only applies to the OR fallback: a strict match keeps ``ts_rank``."""
    _upload(app_with_database, tenant_with_key, _corpus())

    on = _keyword(app_with_database, tenant_with_key, "API documentation page", weighting=True)
    off = _keyword(app_with_database, tenant_with_key, "API documentation page", weighting=False)

    assert on == off and len(on) == 12


def test_a_small_corpus_is_ranked_as_before(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """Below 20 chunks a word's rarity means nothing yet."""
    _upload(app_with_database, tenant_with_key, _corpus(overview=3, filler=2))

    on = _keyword(app_with_database, tenant_with_key, QUESTION, weighting=True)
    off = _keyword(app_with_database, tenant_with_key, QUESTION, weighting=False)

    assert on == off


def test_search_ranks_the_rare_word_chunk_first_in_the_keyword_arm(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        keyword_rarity_weighting=True
    )
    _upload(app_with_database, tenant_with_key, _corpus())
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    hits = client.post(
        "/search", json={"query": QUESTION, "top_k": 10, "mode": "keyword", "explain": True}
    ).json()["results"]

    assert hits[0]["content"] == TARGET


def test_the_setting_can_turn_the_weighting_off(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        keyword_rarity_weighting=False
    )
    _upload(app_with_database, tenant_with_key, _corpus())
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    hits = client.post("/search", json={"query": QUESTION, "top_k": 10, "mode": "keyword"}).json()[
        "results"
    ]

    assert hits[0]["content"] != TARGET


def test_the_weighting_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYWORD_RARITY_WEIGHTING", raising=False)

    assert Settings().keyword_rarity_weighting is True
