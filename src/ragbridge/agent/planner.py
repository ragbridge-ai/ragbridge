"""Decide whether the agent should search again, and for what.

Behind a ``Protocol``, mirroring ``ragbridge.chat.Chatter`` and the other
provider seams, so the loop and tests depend on the shape of a planner, not
on LiteLLM. Tests use ``FakePlanner`` and never call a real provider.

A decision that cannot be understood means "answer now", never an error
(decision 3, docs/plans/phase-4.md): a small model's structured output
cannot be trusted to parse, so a bad reply must degrade to single-shot
retrieval instead of failing the request.
"""

import json
import logging
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

import litellm
from fastapi import Depends

from ragbridge.config import Settings, get_settings

logger = logging.getLogger(__name__)

PLANNER_PROMPT = (
    "You decide whether more searching is needed to answer a question from a "
    "document collection. Reply with a JSON object only, in one of two forms:\n"
    '{"action": "search", "query": "<a new search query>"}\n'
    '{"action": "answer"}\n'
    'Use "search" only if the findings so far do not cover the question, and '
    'make the query different from earlier ones. Otherwise use "answer".'
)


@dataclass(frozen=True)
class PlannerDecision:
    """What to do next: search with ``query``, or answer with what we have."""

    action: Literal["search", "answer"]
    query: str | None = None


ANSWER_NOW = PlannerDecision(action="answer")


class Planner(Protocol):
    async def plan(
        self, question: str, findings: list[str], step: int, max_steps: int
    ) -> PlannerDecision:
        """Decide the next action, given the snippets gathered so far."""
        ...


def parse_decision(raw: str) -> PlannerDecision:
    """Turn a model reply into a decision; anything unusable means answer now."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Planner reply is not valid JSON; answering now: %r", raw[:200])
        return ANSWER_NOW

    if not isinstance(data, dict):
        logger.warning("Planner reply is not a JSON object; answering now: %r", raw[:200])
        return ANSWER_NOW

    action = data.get("action")
    if action == "answer":
        return ANSWER_NOW
    if action != "search":
        logger.warning("Planner reply has a missing or unknown action; answering now: %r", action)
        return ANSWER_NOW

    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        logger.warning("Planner asked to search without a query; answering now: %r", query)
        return ANSWER_NOW
    return PlannerDecision(action="search", query=query.strip())


class LiteLLMPlanner:
    """Plans through LiteLLM, using the planner model or the chat model."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def plan(
        self, question: str, findings: list[str], step: int, max_steps: int
    ) -> PlannerDecision:
        findings_text = "\n\n".join(findings) if findings else "(nothing found yet)"
        messages = [
            {"role": "system", "content": PLANNER_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Findings so far (search {step} of at most {max_steps}):\n{findings_text}"
                ),
            },
        ]

        model = self._settings.agent_planner_model or self._settings.chat_model
        api_base = self._settings.ollama_base_url if model.startswith("ollama/") else None
        response = await litellm.acompletion(model=model, messages=messages, api_base=api_base)
        return parse_decision(response.choices[0].message.content or "")


class FakePlanner:
    """Deterministic planner for tests: returns a script, then keeps answering."""

    def __init__(self, decisions: list[PlannerDecision] | None = None) -> None:
        self._remaining = list(decisions or [])

    async def plan(
        self, question: str, findings: list[str], step: int, max_steps: int
    ) -> PlannerDecision:
        if self._remaining:
            return self._remaining.pop(0)
        return ANSWER_NOW


def get_planner(settings: Annotated[Settings, Depends(get_settings)]) -> Planner:
    """FastAPI dependency returning the real planner.

    Tests override this with a ``FakePlanner`` via
    ``app.dependency_overrides``, the same way ``get_chatter`` is
    overridden.
    """
    return LiteLLMPlanner(settings)
