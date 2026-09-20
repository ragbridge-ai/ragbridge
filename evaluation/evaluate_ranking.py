"""Measure how high the right chunk ranks, on the larger synthetic corpus.

Uses ``ranking_corpus`` (about 100 chunks, mostly generic ones that repeat a
fictional product name) and ``ranking_dataset.jsonl`` (short and long questions,
with and without the product name, one or two facts). It calls ``POST /search``
with ``mode`` vector, keyword and hybrid, so no chat model is involved: only the
embedder. ``--agent`` also runs ``POST /agent`` on the same questions.

Metrics, per question and averaged: ``recall@k`` is the fraction of the expected
chunks found in the first k results, ``complete@k`` is 1 only when *all* of them
were, and ``MRR`` is 1 / the rank of the first expected chunk found (0 if none is).

Run against a live server that has a real embedder (never in CI):

    uv run python -m evaluation.evaluate_ranking --api-key <key> [--agent]

Kept apart from ``evaluate_retrieval`` and the Acme corpus on purpose: that one is
too small to show a ranking change, and its published numbers stay as they are.
"""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from evaluation.evaluate_retrieval import HttpClient
from evaluation.ranking_corpus import build_documents

DATASET_PATH = Path(__file__).parent / "ranking_dataset.jsonl"
HELDOUT_PATH = Path(__file__).parent / "ranking_heldout.jsonl"
"""Questions written after the ranking design was fixed and never used to choose it."""
DOCS_HELDOUT_PATH = Path(__file__).parent / "docs_heldout.jsonl"
"""Held-out questions on real prose: this repository's AGENTS.md and two plan documents.

Run with ``--dataset evaluation/docs_heldout.jsonl --files AGENTS.md
docs/plans/phase-4.md docs/plans/phase-5.md`` against a scratch tenant.
"""
MIXED_DIR = Path(__file__).parent / "mixed"
MIXED_HELDOUT_PATH = Path(__file__).parent / "mixed_heldout.jsonl"
"""Held-out questions on a mixed corpus: an invented CV and three technical documents that
share words with it ("jobs", "Docker", "container"), so a single shared word can pull chunks
from a document the question is not about.

Run with ``--dataset evaluation/mixed_heldout.jsonl --files evaluation/mixed/*.md`` against
a scratch tenant. The corpus is invented; nothing in it is a real person or company.
"""
MIXED_PARAPHRASE_PATH = Path(__file__).parent / "mixed_paraphrase.jsonl"
DOCS_PARAPHRASE_PATH = Path(__file__).parent / "docs_paraphrase.jsonl"
"""Held-out paraphrases: questions that share almost no word with the chunk that answers them
("Which interpreter release does the codebase target?" for a table row ``| Language | Python
3.12 |``). Written before the reranker's window and scoring were measured. Run with ``--files
evaluation/mixed/*.md`` and ``--files AGENTS.md docs/plans/phase-4.md docs/plans/phase-5.md``.
"""
CONTENT_TYPES = {".md": "text/markdown", ".txt": "text/plain", ".pdf": "application/pdf"}
KS = (1, 5, 10)
SEARCH_DEPTH = 20
"""How deep a search looks. Beyond it a chunk counts as not found."""
MODES = ("vector", "keyword", "hybrid")


@dataclass(frozen=True)
class Case:
    id: str
    length: str
    product_name: str
    facts: str
    question: str
    expected: list[str]
    """Verbatim markers, each identifying one expected chunk by text near its start."""


@dataclass(frozen=True)
class CaseResult:
    case: Case
    ranks: list[int | None]
    """The 1-based rank of each expected chunk, or None when it was not found."""

    def recall_at(self, k: int) -> float:
        found = sum(1 for rank in self.ranks if rank is not None and rank <= k)
        return found / len(self.ranks)

    def complete_at(self, k: int) -> float:
        return 1.0 if self.recall_at(k) == 1.0 else 0.0

    @property
    def reciprocal_rank(self) -> float:
        found = [rank for rank in self.ranks if rank is not None]
        return 1 / min(found) if found else 0.0


def load_cases(path: Path = DATASET_PATH) -> list[Case]:
    return [Case(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def upload_corpus(client: HttpClient) -> None:
    for name, text in build_documents().items():
        response = client.post("/documents", files={"file": (name, text.encode(), "text/markdown")})
        response.raise_for_status()


def upload_files(client: HttpClient, paths: list[Path]) -> None:
    """Upload real files instead of the synthetic corpus."""
    for path in paths:
        content_type = CONTENT_TYPES[path.suffix.lower()]
        response = client.post(
            "/documents", files={"file": (path.name, path.read_bytes(), content_type)}
        )
        response.raise_for_status()


def ranks_of(expected: list[str], texts: list[str]) -> list[int | None]:
    """Where each expected marker first appears in ``texts`` (best first), 1-based."""
    return [
        next((position for position, text in enumerate(texts, start=1) if marker in text), None)
        for marker in expected
    ]


def evaluate_search(client: HttpClient, cases: list[Case], mode: str) -> list[CaseResult]:
    results = []
    for case in cases:
        response = client.post(
            "/search", json={"query": case.question, "top_k": SEARCH_DEPTH, "mode": mode}
        )
        response.raise_for_status()
        texts = [hit["content"] for hit in response.json()["results"]]
        results.append(CaseResult(case, ranks_of(case.expected, texts)))
    return results


def evaluate_agent(client: HttpClient, cases: list[Case]) -> tuple[list[CaseResult], list[int]]:
    """Run ``/agent``; rank the expected chunks among its sources, and count its searches."""
    results, steps = [], []
    for case in cases:
        response = client.post("/agent", json={"question": case.question})
        response.raise_for_status()
        body = response.json()
        texts = [source["snippet"] for source in body["sources"]]
        results.append(CaseResult(case, ranks_of(case.expected, texts)))
        steps.append(body["step_count"])
    return results, steps


def summarise(results: list[CaseResult]) -> dict[str, float]:
    n = len(results)
    summary = {f"recall@{k}": sum(r.recall_at(k) for r in results) / n for k in KS}
    summary["complete@5"] = sum(r.complete_at(5) for r in results) / n
    summary["MRR"] = sum(r.reciprocal_rank for r in results) / n
    return summary


def group(results: list[CaseResult], field: str) -> dict[str, list[CaseResult]]:
    groups: dict[str, list[CaseResult]] = {}
    for result in results:
        groups.setdefault(getattr(result.case, field), []).append(result)
    return groups


def format_summary(name: str, results: list[CaseResult]) -> str:
    summary = summarise(results)
    cells = " | ".join(
        f"{summary[key]:.2f}" for key in ("recall@1", "recall@5", "recall@10", "complete@5", "MRR")
    )
    return f"| {name} | {len(results)} | {cells} |"


def format_report(by_mode: dict[str, list[CaseResult]]) -> str:
    header = (
        "| Group | Questions | recall@1 | recall@5 | recall@10 | complete@5 | MRR |\n"
        "|---|---|---|---|---|---|---|"
    )
    lines = []
    for mode, results in by_mode.items():
        lines.append(f"\n### {mode}\n\n{header}")
        lines.append(format_summary("all questions", results))
        for field in ("length", "product_name", "facts"):
            for value, subset in group(results, field).items():
                lines.append(format_summary(f"{field}={value}", subset))
    return "\n".join(lines)


def format_misses(results: list[CaseResult], k: int = 5) -> str:
    rows = []
    for result in results:
        if result.recall_at(k) < 1.0:
            rows.append(f"  {result.case.id:3s} ranks {result.ranks}  {result.case.question}")
    return "\n".join(rows) if rows else "  (none)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", required=True, help="API key created with ragbridge-admin.")
    parser.add_argument("--agent", action="store_true", help="also run POST /agent")
    parser.add_argument("--skip-upload", action="store_true", help="the corpus is already uploaded")
    parser.add_argument(
        "--files",
        type=Path,
        nargs="+",
        help="upload these files instead of the synthetic corpus",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DATASET_PATH,
        help=f"questions to run (the held-out set is {HELDOUT_PATH.name})",
    )
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.api_key}"}
    cases = load_cases(args.dataset)
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=300.0) as client:
        if not args.skip_upload:
            upload_files(client, args.files) if args.files else upload_corpus(client)
        by_mode: dict[str, Any] = {mode: evaluate_search(client, cases, mode) for mode in MODES}
        print(format_report(by_mode))
        print("\nQuestions whose expected chunks are not all in the hybrid top 5:")
        print(format_misses(by_mode["hybrid"]))
        if args.agent:
            agent_results, steps = evaluate_agent(client, cases)
            print("\n### agent (sources, best first)\n")
            print(format_report({"agent": agent_results}).split("\n", 2)[2])
            print(f"\nMean searches per question: {sum(steps) / len(steps):.2f}")
            print("Questions whose expected chunks are not all in the agent's first 5 sources:")
            print(format_misses(agent_results))


if __name__ == "__main__":
    main()
