"""The bounded agent loop: search, ask the planner, maybe search again.

Deliberately small and hand-written (decision 1, docs/plans/phase-4.md).
It knows nothing about the database, embeddings, or the answering model:
retrieval arrives as a ``search`` callable and the loop returns what it
gathered, so it is tested with plain fakes and can be swapped later
behind this one function.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ragbridge.agent.planner import Planner
from ragbridge.retrieval import SearchResult

Search = Callable[[str], Awaitable[list[SearchResult]]]
"""Run one retrieval for a query. Supplied by the caller (see POST /agent)."""

FINDINGS_LIMIT = 5
"""How many of the best chunks found so far the planner is shown."""


@dataclass(frozen=True)
class AgentStep:
    """One search the agent ran: what it asked for and how much came back."""

    query: str
    results: int


@dataclass(frozen=True)
class AgentRun:
    """Everything the loop gathered, ready to be turned into an answer."""

    rows: list[SearchResult]
    """Distinct chunks over all steps, best score first (decision 5)."""
    steps: list[AgentStep]


async def run_agent(question: str, *, planner: Planner, search: Search, max_steps: int) -> AgentRun:
    """Search for ``question``, then let the planner ask for more searches.

    The first search always uses the question itself, so a planner that
    only ever says "answer" (or whose reply cannot be parsed - decision
    3) gives exactly one search, the same as a plain ``POST /query``.
    ``max_steps`` is a hard ceiling checked here, not in a prompt
    (decision 4): the planner is not even asked once the budget is
    spent. At least one search always runs.

    Chunks found more than once are kept once, with their best score
    (decision 5).
    """
    best: dict[uuid.UUID, SearchResult] = {}
    steps: list[AgentStep] = []
    query = question

    while True:
        results = await search(query)
        steps.append(AgentStep(query=query, results=len(results)))
        for row in results:
            chunk_id = row[0].id
            if chunk_id not in best or row[2] > best[chunk_id][2]:
                best[chunk_id] = row

        if len(steps) >= max_steps:
            break
        findings = [chunk.content for chunk, _, _ in _by_score(best)[:FINDINGS_LIMIT]]
        decision = await planner.plan(question, findings, len(steps), max_steps)
        if decision.action != "search" or decision.query is None:
            break
        query = decision.query

    return AgentRun(rows=_by_score(best), steps=steps)


def _by_score(rows: dict[uuid.UUID, SearchResult]) -> list[SearchResult]:
    return sorted(rows.values(), key=lambda row: row[2], reverse=True)
