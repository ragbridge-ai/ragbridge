"""Tests for POST /query."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragbridge.chat import get_chatter
from ragbridge.config import Settings, get_settings
from ragbridge.rerank import FakeReranker, get_reranker


def test_query_ranks_the_exact_matching_chunk_first(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """Default mode is "hybrid" (settings.retrieval_mode), so score is a
    fused RRF score, not 1 - cosine_distance. The exact-match chunk ranks
    first in both arms - the only chunk keyword search matches at all
    (the other document shares no non-stopword terms), and the nearest by
    embedding, since FakeEmbedder gives identical text an identical
    vector. Its score is therefore sum(1 / (k + 1)) over both arms:
    1/61 + 1/61 (docs/plans/phase-2.md, step 2).
    """
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    exact_match = "The mitochondria is the powerhouse of the cell."
    client.post("/documents", files={"file": ("a.txt", exact_match.encode(), "text/plain")})
    other = "Paris is the capital of France."
    client.post("/documents", files={"file": ("b.txt", other.encode(), "text/plain")})

    response = client.post("/query", json={"question": exact_match, "top_k": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["sources"][0]["snippet"] == exact_match
    assert body["sources"][0]["score"] == pytest.approx(1 / 61 + 1 / 61)
    assert body["answer"] == "Fake answer using 2 chunk(s)."


def test_query_mode_vector_reproduces_the_pre_hybrid_ranking(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """mode="vector" runs only vector_search, unfused - score is back to
    1 - cosine_distance, exactly Phase 1's behaviour.
    """
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    exact_match = "The mitochondria is the powerhouse of the cell."
    client.post("/documents", files={"file": ("a.txt", exact_match.encode(), "text/plain")})

    response = client.post("/query", json={"question": exact_match, "mode": "vector"})

    assert response.status_code == 200
    assert response.json()["sources"][0]["score"] == 1.0


def test_query_hybrid_mode_surfaces_a_keyword_match_that_vector_alone_misses(
    app_with_database: FastAPI,
    tenant_with_key: str,
) -> None:
    """The retrieval gap hybrid search closes, end to end through the API.

    A rare literal token (an error code) is not the top vector match for
    itself - FakeEmbedder has no semantics - but hybrid mode still
    surfaces it first, because keyword search matches it exactly and RRF
    rewards a chunk both arms rank well over one arm's own favourite
    (see test_retrieval.py for the arm-level version of this test).
    """
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    error_chunk = "Error code ERR_4021 means the upload exceeded the size limit."
    other_chunk = "Cats are independent and curious animals."
    client.post("/documents", files={"file": ("errors.txt", error_chunk.encode(), "text/plain")})
    client.post("/documents", files={"file": ("cats.txt", other_chunk.encode(), "text/plain")})

    hybrid_response = client.post("/query", json={"question": "ERR_4021", "top_k": 1})
    vector_response = client.post(
        "/query", json={"question": "ERR_4021", "top_k": 1, "mode": "vector"}
    )

    assert hybrid_response.json()["sources"][0]["snippet"] == error_chunk
    assert vector_response.json()["sources"][0]["snippet"] != error_chunk


def test_query_rejects_an_invalid_retrieval_mode(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    response = client.post("/query", json={"question": "Anything?", "mode": "fuzzy"})

    assert response.status_code == 422


def test_query_uses_the_rerankers_order(app_with_database: FastAPI, tenant_with_key: str) -> None:
    """POST /query actually calls the reranker and returns its order.

    With the default NoOpReranker, hybrid mode ranks the error-code chunk
    first for this question (see the hybrid-vs-vector test above).
    FakeReranker reverses whatever it is given, so overriding get_reranker
    with it must flip that order - proof the endpoint uses the reranker's
    output, not retrieval's own order.
    """
    app_with_database.dependency_overrides[get_reranker] = lambda: FakeReranker()
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    error_chunk = "Error code ERR_4021 means the upload exceeded the size limit."
    other_chunk = "Cats are independent and curious animals."
    client.post("/documents", files={"file": ("errors.txt", error_chunk.encode(), "text/plain")})
    client.post("/documents", files={"file": ("cats.txt", other_chunk.encode(), "text/plain")})

    response = client.post("/query", json={"question": "ERR_4021", "top_k": 2})

    snippets = [source["snippet"] for source in response.json()["sources"]]
    assert snippets == [other_chunk, error_chunk]


def test_query_limits_sources_to_top_k(app_with_database: FastAPI, tenant_with_key: str) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    for i in range(3):
        content = f"Fact number {i}.".encode()
        client.post("/documents", files={"file": (f"{i}.txt", content, "text/plain")})

    response = client.post("/query", json={"question": "Fact number 1.", "top_k": 2})

    assert response.status_code == 200
    assert len(response.json()["sources"]) == 2


def test_query_with_no_documents_returns_no_sources(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    response = client.post("/query", json={"question": "Anything?"})

    assert response.status_code == 200
    body = response.json()
    assert body["sources"] == []
    assert body["answer"] == "Fake answer using 0 chunk(s)."


def test_query_source_fields(app_with_database: FastAPI, tenant_with_key: str) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    content = "Some fact about ragbridge."
    files = {"file": ("notes.txt", content.encode(), "text/plain")}
    upload = client.post("/documents", files=files)
    document_id = upload.json()["id"]

    response = client.post("/query", json={"question": content})

    source = response.json()["sources"][0]
    assert source["document_id"] == document_id
    assert source["filename"] == "notes.txt"
    assert source["chunk_index"] == 0
    assert source["snippet"] == content


def test_query_rejects_top_k_above_the_maximum(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})

    response = client.post("/query", json={"question": "Anything?", "top_k": 21})

    assert response.status_code == 422


def test_answer_cache_returns_the_cached_response_without_recomputing(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """With the cache enabled, a second identical query must not call the
    chatter again - proven by swapping in a chatter that would answer
    differently and confirming the response is unchanged, not merely
    that two independent calls happened to agree.
    """
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        answer_cache_enabled=True
    )
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    client.post("/documents", files={"file": ("a.txt", b"Some fact.", "text/plain")})
    first = client.post("/query", json={"question": "Some fact.", "top_k": 5})

    class _StaleMarkerChatter:
        async def answer(self, question: str, context: list[str]) -> str:
            return "STALE - SHOULD NOT BE SEEN"

    app_with_database.dependency_overrides[get_chatter] = lambda: _StaleMarkerChatter()
    second = client.post("/query", json={"question": "Some fact.", "top_k": 5})

    assert second.json() == first.json()
    assert second.json()["answer"] != "STALE - SHOULD NOT BE SEEN"


def test_answer_cache_is_invalidated_by_a_new_upload(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        answer_cache_enabled=True
    )
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    client.post("/documents", files={"file": ("a.txt", b"Some fact.", "text/plain")})
    client.post("/query", json={"question": "Some fact.", "top_k": 5})

    class _NewChatter:
        async def answer(self, question: str, context: list[str]) -> str:
            return "NEW ANSWER AFTER UPLOAD"

    app_with_database.dependency_overrides[get_chatter] = lambda: _NewChatter()
    client.post("/documents", files={"file": ("b.txt", b"Another fact.", "text/plain")})

    response = client.post("/query", json={"question": "Some fact.", "top_k": 5})

    assert response.json()["answer"] == "NEW ANSWER AFTER UPLOAD"


def test_answer_cache_disabled_by_default_recomputes_every_time(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {tenant_with_key}"})
    client.post("/documents", files={"file": ("a.txt", b"Some fact.", "text/plain")})
    client.post("/query", json={"question": "Some fact.", "top_k": 5})

    class _NewChatter:
        async def answer(self, question: str, context: list[str]) -> str:
            return "RECOMPUTED"

    app_with_database.dependency_overrides[get_chatter] = lambda: _NewChatter()
    response = client.post("/query", json={"question": "Some fact.", "top_k": 5})

    assert response.json()["answer"] == "RECOMPUTED"


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


ERROR_CHUNK = "Error code ERR_4021 means the upload exceeded the size limit."
OTHER_CHUNK = "Cats are independent and curious animals."


def _upload_two_chunks(client: TestClient) -> None:
    client.post("/documents", files={"file": ("errors.txt", ERROR_CHUNK.encode(), "text/plain")})
    client.post("/documents", files={"file": ("cats.txt", OTHER_CHUNK.encode(), "text/plain")})


def test_query_without_explain_has_no_retrieval_fields(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _upload_two_chunks(client)

    body = client.post("/query", json={"question": ERROR_CHUNK}).json()

    assert set(body) == {"answer", "sources"}
    for source in body["sources"]:
        assert set(source) == {"document_id", "filename", "chunk_index", "snippet", "score"}


def test_query_explain_reports_which_arm_found_each_source(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _upload_two_chunks(client)

    body = client.post("/query", json={"question": ERROR_CHUNK, "explain": True}).json()

    error_source, cats_source = body["sources"]
    assert error_source["retrieval"] == {
        "vector_rank": 1,
        "keyword_rank": 1,
        "fused_score": pytest.approx(1 / 61 + 1 / 61),
        "rank_before_rerank": 1,
    }
    assert cats_source["retrieval"]["keyword_rank"] is None
    assert cats_source["retrieval"]["vector_rank"] == 2
    # The rest of the response is exactly what it is without explain.
    assert error_source["score"] == pytest.approx(1 / 61 + 1 / 61)
    assert body["answer"] == "Fake answer using 2 chunk(s)."


def test_query_explain_shows_what_reranking_changed(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_reranker] = lambda: FakeReranker()
    client = _client(app_with_database, tenant_with_key)
    _upload_two_chunks(client)

    body = client.post("/query", json={"question": ERROR_CHUNK, "explain": True}).json()

    assert [source["snippet"] for source in body["sources"]] == [OTHER_CHUNK, ERROR_CHUNK]
    assert [source["retrieval"]["rank_before_rerank"] for source in body["sources"]] == [2, 1]


def test_query_explain_does_not_leak_another_tenants_chunks(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    _upload_two_chunks(_client(app_with_database, tenant_with_key))

    body = (
        _client(app_with_database, second_tenant_with_key)
        .post("/query", json={"question": ERROR_CHUNK, "explain": True})
        .json()
    )

    assert body["sources"] == []


def test_answer_cache_keeps_explained_and_plain_answers_apart(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """Whichever request comes first must not decide what the other one gets."""
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        answer_cache_enabled=True
    )
    client = _client(app_with_database, tenant_with_key)
    _upload_two_chunks(client)
    plain_request = {"question": ERROR_CHUNK}
    explain_request = {"question": ERROR_CHUNK, "explain": True}

    client.post("/query", json=plain_request)
    explained = client.post("/query", json=explain_request).json()
    assert "retrieval" in explained["sources"][0]

    # And the other way round, on a question nobody has asked yet.
    other_explain = {"question": OTHER_CHUNK, "explain": True}
    other_plain = {"question": OTHER_CHUNK}
    client.post("/query", json=other_explain)
    plain = client.post("/query", json=other_plain).json()
    assert "retrieval" not in plain["sources"][0]


def test_a_cached_explained_answer_matches_the_fresh_one(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """A cache hit must keep the ``null`` ranks, and a plain one must not grow them."""
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        answer_cache_enabled=True
    )
    client = _client(app_with_database, tenant_with_key)
    _upload_two_chunks(client)

    for request in ({"question": ERROR_CHUNK, "explain": True}, {"question": ERROR_CHUNK}):
        fresh = client.post("/query", json=request)
        cached = client.post("/query", json=request)
        assert cached.json() == fresh.json()


PARAGRAPHS = [
    "Alpha section: the first block of a document, written long enough that it stays a"
    " chunk of its own instead of being merged with a neighbour.",
    "Bravo section: the second block of a document, written long enough that it stays a"
    " chunk of its own instead of being merged with a neighbour.",
    "Charlie section: the third block of a document, written long enough that it stays a"
    " chunk of its own instead of being merged with a neighbour.",
    "Delta section: the fourth block of a document, written long enough that it stays a"
    " chunk of its own instead of being merged with a neighbour.",
]
assert all(len(paragraph) > 120 for paragraph in PARAGRAPHS)


class _RecordingChatter:
    """Keeps the context it was given, so a test can assert on it."""

    def __init__(self) -> None:
        self.context: list[str] = []

    async def answer(self, question: str, context: list[str]) -> str:
        self.context = context
        return "recorded"


def test_query_gives_the_model_one_excerpt_in_document_order_but_sources_in_score_order(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """The model must read a document top to bottom, so a bullet stays under its own
    heading; the response keeps ranking chunks by relevance.
    """
    recorder = _RecordingChatter()
    app_with_database.dependency_overrides[get_chatter] = lambda: recorder
    client = _client(app_with_database, tenant_with_key)
    files = {"file": ("doc.txt", "\n\n".join(PARAGRAPHS).encode(), "text/plain")}
    assert client.post("/documents", files=files).status_code == 201

    body = client.post("/query", json={"question": "Which section is which?", "top_k": 5}).json()

    source_order = [source["chunk_index"] for source in body["sources"]]
    assert sorted(source_order) != source_order, "precondition: retrieval is not in document order"
    # All four chunks are neighbours, so they reach the model as one continuous excerpt.
    assert recorder.context == ["[doc.txt, chunks 0-3]\n" + "\n".join(PARAGRAPHS)]
    assert sorted(source_order) == [0, 1, 2, 3]


SIX = [
    f"Section {name}: a block of a document, written long enough that it stays a chunk of its"
    f" own instead of being merged with a neighbour, number {index}."
    for index, name in enumerate(["One", "Two", "Three", "Four", "Five", "Six"])
]


def _query_top_one(app: FastAPI, key: str, settings: Settings) -> tuple[list[str], list[int]]:
    """Ask for one chunk; return what the model received and which chunk was the source."""
    recorder = _RecordingChatter()
    app.dependency_overrides[get_chatter] = lambda: recorder
    app.dependency_overrides[get_settings] = lambda: settings
    client = _client(app, key)
    files = {"file": ("doc.txt", "\n\n".join(SIX).encode(), "text/plain")}
    assert client.post("/documents", files=files).status_code == 201
    body = client.post("/query", json={"question": "Which section?", "top_k": 1}).json()
    return recorder.context, [source["chunk_index"] for source in body["sources"]]


def test_query_gives_the_model_the_neighbours_of_the_retrieved_chunk_as_one_excerpt(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    context, sources = _query_top_one(app_with_database, tenant_with_key, Settings())

    [index] = sources
    first, last = max(0, index - 1), min(len(SIX) - 1, index + 1)
    span = f"chunk {first}" if first == last else f"chunks {first}-{last}"
    assert context == [f"[doc.txt, {span}]\n" + "\n".join(SIX[first : last + 1])]
    assert len(sources) == 1, "the response still lists only what retrieval returned"


def test_neighbours_can_be_turned_off(app_with_database: FastAPI, tenant_with_key: str) -> None:
    context, sources = _query_top_one(
        app_with_database, tenant_with_key, Settings(answer_context_neighbours=0)
    )

    [index] = sources
    assert context == [f"[doc.txt, chunk {index}]\n{SIX[index]}"]


def test_neighbours_are_dropped_when_the_character_budget_is_too_small(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    context, sources = _query_top_one(
        app_with_database, tenant_with_key, Settings(answer_context_max_chars=10)
    )

    [index] = sources
    assert context == [f"[doc.txt, chunk {index}]\n{SIX[index]}"]
