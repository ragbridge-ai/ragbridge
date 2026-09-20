"""Tests for ChatReranker: the chat model rates chunks, the ratings reorder them.

``litellm.acompletion`` is replaced, so no model is called (Phase 1 decision 5). The fake judge
reads the rating for a passage out of a dict, keyed by a word in the passage.
"""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from ragbridge.config import Settings
from ragbridge.db.models import Chunk, Document
from ragbridge.rerank import (
    JUDGE_TIMEOUT_SECONDS,
    MAX_PARALLEL_JUDGEMENTS,
    ChatReranker,
    LiteLLMReranker,
    NoOpReranker,
    get_reranker,
)
from ragbridge.retrieval import SearchResult

DOCUMENT = Document(
    id=uuid.uuid4(), filename="a.txt", content_type="text/plain", sha256="x", content="x"
)


def _candidates(*texts: str) -> list[SearchResult]:
    """Fused candidates, best first, with descending RRF-like scores."""
    return [
        (
            Chunk(
                id=uuid.uuid4(),
                document_id=DOCUMENT.id,
                chunk_index=index,
                content=text,
                metadata_={},
            ),
            DOCUMENT,
            0.03 - index / 1000,
        )
        for index, text in enumerate(texts)
    ]


class _Judge:
    """Replaces ``litellm.acompletion``: rates a passage by the word it contains."""

    def __init__(self, ratings: dict[str, str | Exception]) -> None:
        self.ratings = ratings
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        prompt = kwargs["messages"][1]["content"]
        reply = next(value for word, value in self.ratings.items() if word in prompt)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


def _rerank(
    monkeypatch: pytest.MonkeyPatch,
    judge: _Judge,
    candidates: list[SearchResult],
    *,
    top_k: int = 5,
    settings: Settings | None = None,
) -> list[SearchResult]:
    monkeypatch.setattr(litellm, "acompletion", judge)
    reranker = ChatReranker(settings or Settings())
    return asyncio.run(reranker.rerank("Which framework?", candidates, top_k))


def _texts(rows: list[SearchResult]) -> list[str]:
    return [chunk.content for chunk, _, _ in rows]


def test_chunks_are_ordered_by_the_rating_the_model_gave(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = _candidates("alpha", "bravo", "charlie")
    judge = _Judge({"alpha": "0", "bravo": "1", "charlie": "2"})

    result = _rerank(monkeypatch, judge, candidates)

    assert _texts(result) == ["charlie", "bravo", "alpha"]


def test_chunks_with_the_same_rating_keep_their_fused_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that rates everything alike must change nothing."""
    candidates = _candidates("alpha", "bravo", "charlie")

    result = _rerank(monkeypatch, _Judge({"alpha": "2", "bravo": "2", "charlie": "2"}), candidates)

    assert _texts(result) == ["alpha", "bravo", "charlie"]


def test_scores_fall_with_the_rating_and_the_list_stays_sorted_best_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _candidates("alpha", "bravo", "charlie")

    result = _rerank(monkeypatch, _Judge({"alpha": "0", "bravo": "1", "charlie": "2"}), candidates)

    scores = [score for _, _, score in result]
    assert scores == pytest.approx([1.0, 2 / 3, 1 / 3])
    assert scores == sorted(scores, reverse=True)


def test_only_the_best_candidates_are_rated_and_the_rest_follow_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _candidates("alpha", "bravo", "charlie", "delta")
    judge = _Judge({"alpha": "0", "bravo": "2", "charlie": "2", "delta": "2"})

    result = _rerank(monkeypatch, judge, candidates, settings=Settings(rerank_candidates=2))

    assert len(judge.calls) == 2, "charlie and delta are never shown to the model"
    assert _texts(result) == ["bravo", "alpha", "charlie", "delta"]
    assert [score for _, _, score in result][2:] == [0.0, 0.0]


def test_the_result_is_cut_to_top_k(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = _candidates("alpha", "bravo", "charlie")

    result = _rerank(
        monkeypatch, _Judge({"alpha": "0", "bravo": "1", "charlie": "2"}), candidates, top_k=2
    )

    assert _texts(result) == ["charlie", "bravo"]


def test_no_candidates_means_no_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    judge = _Judge({})

    assert _rerank(monkeypatch, judge, []) == []
    assert judge.calls == []


def test_a_reply_without_a_digit_counts_as_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    """Between a 2 and a 0: not pushed up, not pushed down."""
    candidates = _candidates("alpha", "bravo", "charlie")
    judge = _Judge({"alpha": "0", "bravo": "Yes, it does.", "charlie": "2"})

    result = _rerank(monkeypatch, judge, candidates)

    assert _texts(result) == ["charlie", "bravo", "alpha"]


def test_a_digit_inside_a_longer_reply_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = _candidates("alpha", "bravo")

    result = _rerank(monkeypatch, _Judge({"alpha": "Rating: 0", "bravo": "Rating: 2"}), candidates)

    assert _texts(result) == ["bravo", "alpha"]


def test_a_failing_model_call_costs_only_that_chunk_its_rating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One transient error out of several must not throw the other ratings away."""
    candidates = _candidates("alpha", "bravo", "charlie")
    judge = _Judge({"alpha": "2", "bravo": RuntimeError("model down"), "charlie": "2"})

    result = _rerank(monkeypatch, judge, candidates, top_k=3)

    assert _texts(result) == ["alpha", "charlie", "bravo"], "bravo counts as neutral, 1 below a 2"
    assert [score for _, _, score in result] == pytest.approx([1.0, 1.0, 2 / 3])


def test_when_every_call_fails_the_fused_order_and_scores_are_returned_untouched(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    candidates = _candidates("alpha", "bravo", "charlie")
    down = RuntimeError("model down")

    with caplog.at_level("WARNING"):
        result = _rerank(
            monkeypatch, _Judge({"alpha": down, "bravo": down, "charlie": down}), candidates
        )

    assert result == candidates
    assert "no usable rating" in caplog.text


def test_when_no_reply_has_a_rating_the_fused_order_is_kept_and_a_warning_says_why(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A model that thinks first runs out of the judge's few tokens: '<think>' and nothing else."""
    candidates = _candidates("alpha", "bravo")

    with caplog.at_level("WARNING"):
        result = _rerank(monkeypatch, _Judge({"alpha": "<think>Let me", "bravo": ""}), candidates)

    assert result == candidates
    assert "thinks first" in caplog.text


@pytest.mark.parametrize("reply", ["2", "**2**", "Rating: 2", "2 = yes, the answer is in it"])
def test_a_reply_that_starts_with_the_rating_is_read(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    candidates = _candidates("alpha", "bravo", "charlie")

    result = _rerank(
        monkeypatch, _Judge({"alpha": reply, "bravo": "0", "charlie": "2"}), candidates
    )

    assert _texts(result) == ["alpha", "charlie", "bravo"], "alpha ties charlie and comes first"


@pytest.mark.parametrize(
    "reply", ["20/20", "Passage 2 says nothing about it... 0", "3", "", "Yes."]
)
def test_a_reply_that_does_not_start_with_the_rating_counts_as_neutral(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    """A digit further into a chatty reply, or the start of "20/20", is not the rating; reading it
    anyway would reorder chunks silently. (These replies are chosen so that a loose "first digit
    anywhere" reading gives 2 or 0, not the neutral 1, and the test can tell.)
    """
    candidates = _candidates("alpha", "bravo", "charlie")

    result = _rerank(
        monkeypatch, _Judge({"alpha": reply, "bravo": "0", "charlie": "2"}), candidates
    )

    assert _texts(result) == ["charlie", "alpha", "bravo"], "alpha is neutral: 1, between 2 and 0"


def test_judge_calls_in_flight_are_limited_across_requests_not_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six chunks for each of three requests at once must never mean more than the limit."""
    in_flight = 0
    peak = 0

    async def acompletion(**kwargs: Any) -> Any:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="2"))])

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    reranker = ChatReranker(Settings(rerank_candidates=6))

    async def three_requests() -> None:
        await asyncio.gather(*(reranker.rerank("q", _candidates(*"abcdef"), 3) for _ in range(3)))

    asyncio.run(three_requests())

    assert peak == MAX_PARALLEL_JUDGEMENTS


def test_every_judge_call_has_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    judge = _Judge({"alpha": "2"})

    _rerank(monkeypatch, judge, _candidates("alpha"))

    assert judge.calls[0]["timeout"] == JUDGE_TIMEOUT_SECONDS


def test_the_judge_runs_at_temperature_zero_on_the_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    judge = _Judge({"alpha": "2"})

    _rerank(monkeypatch, judge, _candidates("alpha"), settings=Settings(chat_model="ollama/x"))

    [call] = judge.calls
    assert call["model"] == "ollama/x"
    assert call["temperature"] == 0
    assert call["api_base"] == Settings().ollama_base_url


def test_a_separate_judge_model_can_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    judge = _Judge({"alpha": "2"})
    settings = Settings(chat_model="ollama/x", rerank_chat_model="anthropic/judge")

    _rerank(monkeypatch, judge, _candidates("alpha"), settings=settings)

    [call] = judge.calls
    assert call["model"] == "anthropic/judge"
    assert call["api_base"] is None, "only an Ollama model gets the Ollama address"


def test_the_prompt_holds_the_question_and_the_passage(monkeypatch: pytest.MonkeyPatch) -> None:
    judge = _Judge({"alpha": "2"})

    _rerank(monkeypatch, judge, _candidates("alpha passage text"))

    prompt = judge.calls[0]["messages"][1]["content"]
    assert "Which framework?" in prompt and "alpha passage text" in prompt


def test_a_very_long_passage_is_cut_before_it_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    judge = _Judge({"alpha": "2"})

    _rerank(monkeypatch, judge, _candidates("alpha" + "x" * 10_000))

    assert len(judge.calls[0]["messages"][1]["content"]) < 4_000


def test_the_backend_setting_picks_the_reranker() -> None:
    assert isinstance(get_reranker(Settings()), NoOpReranker)
    assert isinstance(get_reranker(Settings(rerank_enabled=True)), LiteLLMReranker)
    assert isinstance(
        get_reranker(Settings(rerank_enabled=True, rerank_backend="chat")), ChatReranker
    )
    assert isinstance(get_reranker(Settings(rerank_backend="chat")), NoOpReranker), "off is off"


def test_the_number_of_rated_candidates_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        Settings(rerank_candidates=0)
