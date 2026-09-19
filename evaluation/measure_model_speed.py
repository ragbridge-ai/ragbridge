"""Measure how fast a machine runs an Ollama model, using Ollama's own counters.

Model quality is measured by ``evaluate_qa.py`` and ``evaluate_agent.py``; this is
the other half of choosing a model: what it costs in time and memory *on this
hardware*. Nothing here involves ragbridge - it talks to Ollama directly, so the
numbers are the machine's and the model's, not the application's.

For each model it reports:

    load          seconds to load the model into memory from cold
    memory        size when loaded, and the share of it on the GPU (100% means the
                  whole model fits there; less means part runs on the slower CPU)
    writes        tokens per second while generating an answer (median of three)
    reads         tokens per second while reading a RAG-sized prompt (about 1,100
                  tokens, like five retrieved chunks), and the total time of one
                  such call - what a real ``/query`` costs the model

Run it on the server you plan to deploy to, not only on a laptop:

    uv run python -m evaluation.measure_model_speed llama3.2 qwen2.5:7b

Ollama measures time in nanoseconds; the counters used are ``eval_count`` and
``eval_duration`` (generation), ``prompt_eval_*`` (reading) and ``load_duration``.
"""

import argparse
import json
import statistics
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).parent / "corpus"
SHORT_PROMPT = "Explain in about 150 words how a library catalogue helps people find books."
NS = 1e9


def tokens_per_second(token_count: int, duration_ns: int) -> float:
    """Tokens per second from Ollama's counters; 0 when nothing was timed."""
    return token_count / (duration_ns / NS) if duration_ns else 0.0


@dataclass(frozen=True)
class Speed:
    model: str
    load_seconds: float
    size_gb: float
    gpu_percent: float
    writes_tps: float
    reads_tps: float
    rag_prompt_tokens: int
    rag_call_seconds: float


def _post(base_url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url + path, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        result: dict[str, Any] = json.load(response)
    return result


def generate(base_url: str, model: str, prompt: str, tokens: int) -> dict[str, Any]:
    started = time.perf_counter()
    result = _post(
        base_url,
        "/api/generate",
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "5m",
            "options": {"num_predict": tokens, "temperature": 0},
        },
    )
    result["wall_seconds"] = time.perf_counter() - started
    return result


def loaded_model(base_url: str, model: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + "/api/ps", timeout=30) as response:
        models: list[dict[str, Any]] = json.load(response)["models"]
    return next((m for m in models if m["name"].startswith(model)), models[0])


def rag_sized_prompt() -> str:
    """The whole evaluation corpus (~1,100 tokens) plus a question."""
    context = "\n\n".join(path.read_text() for path in sorted(CORPUS_DIR.glob("*.md")))
    return (
        "Answer using only the context.\n\nContext:\n"
        f"{context}\n\nQuestion: How many days of paid vacation do employees get?"
    )


def measure(base_url: str, model: str) -> Speed:
    subprocess.run(["ollama", "stop", model], capture_output=True)  # start cold
    time.sleep(2)
    cold = generate(base_url, model, SHORT_PROMPT, 8)
    info = loaded_model(base_url, model)
    size = info["size"]
    warm = [generate(base_url, model, SHORT_PROMPT, 150) for _ in range(3)]
    rag = generate(base_url, model, rag_sized_prompt(), 40)
    subprocess.run(["ollama", "stop", model], capture_output=True)
    return Speed(
        model=model,
        load_seconds=cold["load_duration"] / NS,
        size_gb=size / 1e9,
        gpu_percent=100 * info.get("size_vram", 0) / size if size else 0.0,
        writes_tps=statistics.median(
            tokens_per_second(w["eval_count"], w["eval_duration"]) for w in warm
        ),
        reads_tps=tokens_per_second(rag["prompt_eval_count"], rag["prompt_eval_duration"]),
        rag_prompt_tokens=rag["prompt_eval_count"],
        rag_call_seconds=rag["wall_seconds"],
    )


def format_table(results: list[Speed]) -> str:
    lines = [
        "| Model | Writes (tokens/s) | Reads a RAG prompt (tokens/s) | One RAG call | "
        "Cold load | Memory when loaded | On the GPU |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.model} | {r.writes_tps:.0f} | {r.reads_tps:.0f} | {r.rag_call_seconds:.1f} s "
            f"(~{r.rag_prompt_tokens} tokens) | {r.load_seconds:.1f} s | {r.size_gb:.2f} GB | "
            f"{r.gpu_percent:.0f}% |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="+", help="Ollama model tags, e.g. llama3.2 qwen2.5:7b")
    parser.add_argument("--base-url", default="http://localhost:11434")
    args = parser.parse_args()
    print(format_table([measure(args.base_url, model) for model in args.models]))


if __name__ == "__main__":
    main()
