"""Compare POST /agent with POST /query on questions that need more than one search.

The Phase 2 evaluation corpus is six documents of under 1 KB each - roughly one
chunk apiece - so a single ``/query`` at ``top_k=5`` already returns 5 of the 6
chunks. Retrieval there is capped near 100%, which leaves an agent nothing to
improve, and running ``/agent`` on it would only produce a meaningless tie.

This builds a corpus where the answer *cannot* be found from the question alone.
Each chain has four short documents:

    Project Zephyron is owned by Marek Sorvath.         (project -> owner)
    Marek Sorvath reports to Lunia Brenik.              (owner -> manager)
    The email address of Marek Sorvath is m.sorvath@... (owner's email)
    The email address of Lunia Brenik is l.brenik@...   (manager's email)

and asks, at three depths:

    1-hop  What is the email address of Marek Sorvath?
    2-hop  What is the email address of the person who owns Project Zephyron?
    3-hop  What is the email address of the manager of the person who owns
           Project Zephyron?

The names are invented, so no model can answer from what it already knows, and the
right answer is an exact string, so scoring needs no LLM judge (ADR 0004 found the
default model unreliable as one). A question is *retrieved* if the document holding
the answer appears among the returned sources, and *correct* if the answer text
contains the email address.

Run against a live server with a real embedder and chat model configured:

    uv run python -m evaluation.evaluate_agent --base-url http://localhost:8000 --api-key <key>

CI never does this (Phase 1 decision 5); only the unit tests run there.
"""

import argparse
import random
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

Tier = Literal["1-hop", "2-hop", "3-hop"]
Endpoint = Literal["query", "agent"]
TIERS: tuple[Tier, ...] = ("1-hop", "2-hop", "3-hop")

FIRST_SYLLABLES = ["Vel", "Mar", "Kor", "Tal", "Bren", "Sor", "Dax", "Lun", "Fen", "Qui", "Rav"]
FIRST_ENDINGS = ["in", "ek", "ia", "os", "ith", "ara", "un", "el"]
LAST_SYLLABLES = ["Sorv", "Brenn", "Kald", "Mirov", "Tesk", "Ulan", "Varn", "Holt", "Drev", "Yask"]
LAST_ENDINGS = ["ath", "ik", "ov", "ane", "uk", "eth", "ard"]
PROJECT_SYLLABLES = ["Zeph", "Aur", "Nim", "Cal", "Orl", "Pyr", "Thal", "Vex", "Quor", "Ilm"]
PROJECT_ENDINGS = ["yron", "elis", "bus", "adon", "ix", "ora", "ium"]


class HttpResponse(Protocol):
    def raise_for_status(self) -> object: ...
    def json(self) -> Any: ...


class HttpClient(Protocol):
    def post(self, url: str, *, json: Any = None, files: Any = None) -> HttpResponse: ...


@dataclass(frozen=True)
class Case:
    tier: Tier
    question: str
    answer_email: str
    """The exact string a correct answer contains."""
    answer_document: str
    """The filename of the document that holds ``answer_email``."""


@dataclass(frozen=True)
class Corpus:
    documents: dict[str, str]
    """Filename -> text."""
    cases: list[Case]


@dataclass
class TierScore:
    asked: int = 0
    retrieved: int = 0
    correct: int = 0
    steps: list[int] = field(default_factory=list)

    @property
    def retrieved_rate(self) -> float:
        return self.retrieved / self.asked if self.asked else 0.0

    @property
    def correct_rate(self) -> float:
        return self.correct / self.asked if self.asked else 0.0

    @property
    def mean_steps(self) -> float | None:
        return sum(self.steps) / len(self.steps) if self.steps else None


def _unique_names(
    rng: random.Random, starts: list[str], ends: list[str], count: int, *, taken: set[str]
) -> list[str]:
    names: list[str] = []
    while len(names) < count:
        name = rng.choice(starts) + rng.choice(ends)
        if name not in taken:
            taken.add(name)
            names.append(name)
    return names


def build_corpus(chains: int, seed: int = 7) -> Corpus:
    """Build ``chains`` four-document chains and three questions per chain."""
    rng = random.Random(seed)
    taken: set[str] = set()
    firsts = _unique_names(rng, FIRST_SYLLABLES, FIRST_ENDINGS, chains * 2, taken=taken)
    lasts = _unique_names(rng, LAST_SYLLABLES, LAST_ENDINGS, chains * 2, taken=set())
    projects = _unique_names(rng, PROJECT_SYLLABLES, PROJECT_ENDINGS, chains, taken=set())

    documents: dict[str, str] = {}
    cases: list[Case] = []
    for index in range(chains):
        owner = f"{firsts[index * 2]} {lasts[index * 2]}"
        manager = f"{firsts[index * 2 + 1]} {lasts[index * 2 + 1]}"
        project = projects[index]
        owner_email = f"{owner.lower().replace(' ', '.')}@example.test"
        manager_email = f"{manager.lower().replace(' ', '.')}@example.test"

        documents[f"project-{index}.txt"] = f"Project {project} is owned by {owner}."
        documents[f"reports-{index}.txt"] = f"{owner} reports to {manager}."
        documents[f"email-owner-{index}.txt"] = f"The email address of {owner} is {owner_email}."
        documents[f"email-manager-{index}.txt"] = (
            f"The email address of {manager} is {manager_email}."
        )

        owner_doc = f"email-owner-{index}.txt"
        cases += [
            Case("1-hop", f"What is the email address of {owner}?", owner_email, owner_doc),
            Case(
                "2-hop",
                f"What is the email address of the person who owns Project {project}?",
                owner_email,
                owner_doc,
            ),
            Case(
                "3-hop",
                "What is the email address of the manager of the person who owns "
                f"Project {project}?",
                manager_email,
                f"email-manager-{index}.txt",
            ),
        ]
    return Corpus(documents=documents, cases=cases)


def upload_corpus(client: HttpClient, corpus: Corpus) -> None:
    for filename, text in corpus.documents.items():
        response = client.post(
            "/documents", files={"file": (filename, text.encode(), "text/plain")}
        )
        response.raise_for_status()


def ask(client: HttpClient, endpoint: Endpoint, question: str) -> dict[str, Any]:
    if endpoint == "agent":
        response = client.post("/agent", json={"question": question})
    else:
        response = client.post("/query", json={"question": question, "top_k": 5})
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


def evaluate(
    client: HttpClient, corpus: Corpus, endpoint: Endpoint, *, repeats: int = 1
) -> dict[Tier, TierScore]:
    """Ask every case ``repeats`` times through ``endpoint`` and score it per tier."""
    scores = {tier: TierScore() for tier in TIERS}
    for _ in range(repeats):
        for case in corpus.cases:
            body = ask(client, endpoint, case.question)
            score = scores[case.tier]
            score.asked += 1
            if any(source["filename"] == case.answer_document for source in body["sources"]):
                score.retrieved += 1
            if case.answer_email.lower() in body["answer"].lower():
                score.correct += 1
            if endpoint == "agent":
                score.steps.append(body["step_count"])
    return scores


def format_table(results: dict[Endpoint, dict[Tier, TierScore]]) -> str:
    lines = [
        "| Endpoint | Tier | Questions | Answer document retrieved | Answer correct | Mean steps |",
        "|---|---|---|---|---|---|",
    ]
    for endpoint, scores in results.items():
        for tier in TIERS:
            score = scores[tier]
            steps = f"{score.mean_steps:.2f}" if score.mean_steps is not None else "-"
            lines.append(
                f"| `/{endpoint}` | {tier} | {score.asked} | "
                f"{score.retrieved_rate:.0%} ({score.retrieved}/{score.asked}) | "
                f"{score.correct_rate:.0%} ({score.correct}/{score.asked}) | {steps} |"
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", required=True, help="API key created with ragbridge-admin.")
    parser.add_argument("--chains", type=int, default=15, help="Chains of four documents each.")
    parser.add_argument("--repeats", type=int, default=3, help="Times each question is asked.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--skip-upload", action="store_true", help="Corpus is already uploaded.")
    args = parser.parse_args()

    corpus = build_corpus(args.chains, args.seed)
    headers = {"Authorization": f"Bearer {args.api_key}"}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=300.0) as client:
        if not args.skip_upload:
            upload_corpus(client, corpus)
        results: dict[Endpoint, dict[Tier, TierScore]] = {
            "query": evaluate(client, corpus, "query", repeats=args.repeats),
            "agent": evaluate(client, corpus, "agent", repeats=args.repeats),
        }

    print(
        f"{len(corpus.documents)} documents, {len(corpus.cases)} questions x {args.repeats} runs\n"
    )
    print(format_table(results))


if __name__ == "__main__":
    main()
