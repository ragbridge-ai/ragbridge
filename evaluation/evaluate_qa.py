"""Compare chat models on question answering, with no LLM judge.

RAGAS needs a judge model, and ADR 0004 found the default one unreliable at it.
This scores answers *mechanically*, so any chat model can be compared fairly and
repeatably. 43 questions over the Phase 2 corpus, in three kinds:

    lookup      the 25 Phase 2 questions. Easy; most models pass. The answer must
                contain the key fact (a number, a name, an address).
    paraphrase  10 questions that share almost no keywords with the text ("how
                often do I have to replace my password?" for "changed every 180
                days"). This is where a small model's weakness shows.
    abstain     8 questions the documents do not answer. The right response is to
                say so; inventing an answer is the failure.

A question passes if every ``must_match`` regular expression matches the answer
and no ``must_not_match`` does; an abstain question passes if the answer says it
does not know. Also reported: latency, and how often an *answerable* question was
wrongly met with "I don't know" - usually a sign retrieval found the passage but
the model did not connect it to the question.

Which chat model answers is configured on the server (``CHAT_MODEL``), so this
takes ``--label`` to name it in the output. Run it against a live server:

    uv run python -m evaluation.evaluate_qa --api-key <key> --label ollama/qwen2.5:7b

CI never does this (Phase 1 decision 5); only the unit tests run there.
"""

import argparse
import json
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from evaluation.evaluate_retrieval import CORPUS_DIR, HttpClient

CHECKS_PATH = Path(__file__).parent / "qa_checks.jsonl"
Category = Literal["lookup", "paraphrase", "abstain"]
CATEGORIES: tuple[Category, ...] = ("lookup", "paraphrase", "abstain")

ABSTAIN_PATTERN = re.compile(
    r"don'?t know|do not know|not sure|"
    r"(?:is|are|was|were)? ?not (?:mentioned|provided|specified|stated|available|included|covered|"
    r"in the (?:context|documents?|provided))|"
    r"no (?:information|mention|details?)|"
    r"cannot (?:find|answer|determine)|can'?t (?:find|answer|determine)|unable to|"
    r"(?:doesn'?t|does not|don'?t|do not) (?:say|mention|specify|provide|contain|include|state)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Check:
    id: str
    category: Category
    question: str
    must_match: tuple[str, ...]
    must_not_match: tuple[str, ...]
    good_example: str
    """An answer that should pass. Unused when scoring; it lets the tests prove
    every check can actually be passed."""


@dataclass
class CategoryScore:
    asked: int = 0
    passed: int = 0
    wrongly_abstained: int = 0
    seconds: list[float] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.passed / self.asked if self.asked else 0.0


def load_checks(path: Path = CHECKS_PATH) -> list[Check]:
    checks = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        checks.append(
            Check(
                id=row["id"],
                category=row["category"],
                question=row["question"],
                must_match=tuple(row["must_match"]),
                must_not_match=tuple(row["must_not_match"]),
                good_example=row["good_example"],
            )
        )
    return checks


def abstained(answer: str) -> bool:
    """Whether the answer says the information is not available."""
    return ABSTAIN_PATTERN.search(answer) is not None


def passes(check: Check, answer: str) -> bool:
    if check.category == "abstain":
        return abstained(answer)
    if not all(re.search(pattern, answer, re.IGNORECASE) for pattern in check.must_match):
        return False
    return not any(re.search(p, answer, re.IGNORECASE) for p in check.must_not_match)


def evaluate(
    client: HttpClient, checks: list[Check], *, top_k: int = 5
) -> dict[Category, CategoryScore]:
    scores = {category: CategoryScore() for category in CATEGORIES}
    for check in checks:
        started = time.perf_counter()
        response = client.post("/query", json={"question": check.question, "top_k": top_k})
        response.raise_for_status()
        elapsed = time.perf_counter() - started
        answer: str = response.json()["answer"]

        score = scores[check.category]
        score.asked += 1
        score.seconds.append(elapsed)
        if passes(check, answer):
            score.passed += 1
        else:
            score.failures.append(f"{check.id} {check.question!r} -> {answer[:140]!r}")
        if check.category != "abstain" and abstained(answer):
            score.wrongly_abstained += 1
    return scores


def format_table(label: str, scores: dict[Category, CategoryScore]) -> str:
    lines = [
        f"Model: {label}",
        "",
        '| Kind | Questions | Passed | Wrongly said "I don\'t know" | Median s | Slowest s |',
        "|---|---|---|---|---|---|",
    ]
    for category in CATEGORIES:
        s = scores[category]
        wrongly = "-" if category == "abstain" else str(s.wrongly_abstained)
        median = statistics.median(s.seconds) if s.seconds else 0.0
        slowest = max(s.seconds) if s.seconds else 0.0
        lines.append(
            f"| {category} | {s.asked} | {s.rate:.0%} ({s.passed}/{s.asked}) | {wrongly} | "
            f"{median:.1f} | {slowest:.1f} |"
        )
    return "\n".join(lines)


def upload_corpus(client: HttpClient) -> None:
    for path in sorted(CORPUS_DIR.glob("*.md")):
        response = client.post(
            "/documents", files={"file": (path.name, path.read_bytes(), "text/markdown")}
        )
        response.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", required=True, help="API key created with ragbridge-admin.")
    parser.add_argument("--label", default="(unnamed)", help="Name of the chat model under test.")
    parser.add_argument("--skip-upload", action="store_true", help="Corpus is already uploaded.")
    parser.add_argument("--show-failures", action="store_true")
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.api_key}"}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=300.0) as client:
        if not args.skip_upload:
            upload_corpus(client)
        scores = evaluate(client, load_checks())

    print(format_table(args.label, scores))
    if args.show_failures:
        for category in CATEGORIES:
            for failure in scores[category].failures:
                print(f"  FAIL [{category}] {failure}")


if __name__ == "__main__":
    main()
