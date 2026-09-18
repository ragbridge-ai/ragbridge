"""Tests for the agent loop, using FakePlanner and a fake search.

No database, no network, no model: the loop only sees a ``search``
callable and a ``Planner``, so both are replaced with in-memory fakes.
"""

import asyncio
import uuid

from ragbridge.agent.loop import AgentRun, AgentStep, run_agent
from ragbridge.agent.planner import FakePlanner, PlannerDecision
from ragbridge.db.models import Chunk, Document
from ragbridge.retrieval import SearchResult

DOCUMENT = Document(
    id=uuid.uuid4(), filename="a.txt", content_type="text/plain", sha256="x", content="x"
)


def _chunk(content: str = "") -> Chunk:
    return Chunk(
        id=uuid.uuid4(), document_id=DOCUMENT.id, chunk_index=0, content=content, metadata_={}
    )


def _row(chunk: Chunk, score: float) -> SearchResult:
    return (chunk, DOCUMENT, score)


class RecordingSearch:
    """A fake ``search`` that returns fresh chunks and remembers its queries."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def __call__(self, query: str) -> list[SearchResult]:
        self.queries.append(query)
        return [_row(_chunk(query), 0.5)]


class SpyPlanner(FakePlanner):
    """A FakePlanner that records the findings it was shown on each call."""

    def __init__(self, decisions: list[PlannerDecision] | None = None) -> None:
        super().__init__(decisions)
        self.findings_seen: list[list[str]] = []

    async def plan(
        self, question: str, findings: list[str], step: int, max_steps: int
    ) -> PlannerDecision:
        self.findings_seen.append(findings)
        return await super().plan(question, findings, step, max_steps)


def _searches(*queries: str) -> list[PlannerDecision]:
    return [PlannerDecision(action="search", query=query) for query in queries]


def _run(planner: FakePlanner, search: RecordingSearch, max_steps: int = 3) -> AgentRun:
    return asyncio.run(run_agent("question", planner=planner, search=search, max_steps=max_steps))


def test_single_step_query_stops_after_one_step() -> None:
    search = RecordingSearch()

    run = _run(FakePlanner(), search)

    assert search.queries == ["question"]
    assert run.steps == [AgentStep(query="question", results=1)]


def test_first_search_uses_the_question_and_later_ones_use_the_planner_query() -> None:
    search = RecordingSearch()

    run = _run(FakePlanner(_searches("second query")), search)

    assert search.queries == ["question", "second query"]
    assert [step.query for step in run.steps] == ["question", "second query"]


def test_step_ceiling_is_respected_even_if_the_planner_never_stops() -> None:
    search = RecordingSearch()
    planner = FakePlanner(_searches(*(f"query {n}" for n in range(10))))

    run = _run(planner, search, max_steps=3)

    assert len(run.steps) == 3
    assert len(search.queries) == 3


def test_planner_is_not_asked_once_the_budget_is_spent() -> None:
    planner = SpyPlanner(_searches(*(f"query {n}" for n in range(10))))

    _run(planner, RecordingSearch(), max_steps=3)

    # Asked after step 1 and step 2; not after step 3.
    assert len(planner.findings_seen) == 2


def test_max_steps_of_one_never_searches_twice() -> None:
    search = RecordingSearch()

    run = _run(FakePlanner(_searches("more")), search, max_steps=1)

    assert len(run.steps) == 1


def test_at_least_one_search_runs_even_with_a_zero_budget() -> None:
    search = RecordingSearch()

    run = _run(FakePlanner(_searches("more")), search, max_steps=0)

    assert len(run.steps) == 1


def test_duplicate_chunks_keep_their_best_score() -> None:
    shared = _chunk("shared")
    other = _chunk("other")
    responses = [[_row(shared, 0.2), _row(other, 0.4)], [_row(shared, 0.9)]]

    async def search(query: str) -> list[SearchResult]:
        return responses.pop(0)

    planner = FakePlanner(_searches("again"))
    run = asyncio.run(run_agent("question", planner=planner, search=search, max_steps=3))

    assert [(chunk.id, score) for chunk, _, score in run.rows] == [
        (shared.id, 0.9),
        (other.id, 0.4),
    ]


def test_a_worse_later_score_does_not_replace_a_better_earlier_one() -> None:
    shared = _chunk("shared")
    responses = [[_row(shared, 0.8)], [_row(shared, 0.1)]]

    async def search(query: str) -> list[SearchResult]:
        return responses.pop(0)

    planner = FakePlanner(_searches("again"))
    run = asyncio.run(run_agent("question", planner=planner, search=search, max_steps=3))

    assert [score for _, _, score in run.rows] == [0.8]


def test_steps_report_result_counts_before_de_duplication() -> None:
    shared = _chunk("shared")
    responses = [[_row(shared, 0.5)], [_row(shared, 0.6), _row(_chunk(), 0.3)]]

    async def search(query: str) -> list[SearchResult]:
        return responses.pop(0)

    planner = FakePlanner(_searches("again"))
    run = asyncio.run(run_agent("question", planner=planner, search=search, max_steps=3))

    assert run.steps == [AgentStep("question", 1), AgentStep("again", 2)]
    assert len(run.rows) == 2


def test_planner_is_shown_the_findings_so_far() -> None:
    planner = SpyPlanner()

    _run(planner, RecordingSearch(), max_steps=3)

    assert planner.findings_seen == [["question"]]
