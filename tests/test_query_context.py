"""What POST /query hands the answer model when a bullet sits at the seam of two chunks.

The invented CV below is one block of lines without blank lines (the way a PDF is
extracted), so the chunker cuts it inside a company's bullets: the Docker bullet is
the last line of one chunk and, through the overlap, near the top of the next - right
above the heading of the *next* company, with its own company's heading one chunk
away. If the model is only given the second chunk, it can read the bullet under the
wrong heading.
"""

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragbridge.chat import get_chatter
from ragbridge.chunking import chunk_text
from ragbridge.config import Settings, get_settings

CV = """\
## Experience
### Brightwave Labs — Rotterdam, 2022–2024
- Led the migration of a monolithic shop to separate services, cutting page load time by 38%.
- Introduced code review rules and a shared style guide across three squads.
- Mentored four junior developers and ran the weekly architecture meeting.
- Negotiated the roadmap with the product owner every sprint and kept the backlog readable.
- Set up the quarterly hack day, where the team tried new tools and shared what worked.
- Wrote the migration guide that eleven teams used to move their data to the new services.
- Ran the yearly planning offsite for sixty people and published the outcome as a roadmap.
### Tessellate Systems — Utrecht, 2019–2022
- Built the invoicing API used by 1,200 customers, with a p95 latency under 120 ms.
- Wrote the on-call runbook and reduced night-time pages by half.
- Containerised the build pipeline with Docker, which cut release time from 50 minutes to 9.
### Corvid Analytics — Ghent, 2017–2019
- Designed the event ingestion service that processed 40 million rows per day.
- Replaced a fragile cron setup with a queue-based job system.
- Presented the data model to the customer success team every quarter.
### Halcyon Retail — Leeds, 2015–2017
- Rebuilt the product search with full-text search, which raised conversion by 6%.
- Added automated tests to the checkout flow and caught 14 regressions before release.
- Trained the support team to read application logs.
"""
SETTINGS = Settings(chunk_size=1000, chunk_overlap=200, chunk_min_size=100)
BULLET = "- Containerised the build pipeline with Docker"


class _RecordingChatter:
    def __init__(self) -> None:
        self.context: list[str] = []

    async def answer(self, question: str, context: list[str]) -> str:
        self.context = context
        return "recorded"


def _seam_chunk() -> tuple[int, str]:
    """The chunk that holds the Docker bullet without its company's heading, and its index."""
    chunks = chunk_text(
        CV,
        chunk_size=SETTINGS.chunk_size,
        chunk_overlap=SETTINGS.chunk_overlap,
        min_size=SETTINGS.chunk_min_size,
    )
    [(index, chunk)] = [
        (i, c) for i, c in enumerate(chunks) if BULLET in c and "### Tessellate" not in c
    ]
    assert index > 0 and "### Corvid" in chunk, "precondition: the wrong heading follows it"
    return index, chunk


def _context_for_the_seam_chunk(
    app: FastAPI, key: str, settings: Settings
) -> tuple[int, list[str]]:
    recorder = _RecordingChatter()
    app.dependency_overrides[get_chatter] = lambda: recorder
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    files = {"file": ("cv.md", CV.encode(), "text/markdown")}
    assert client.post("/documents", files=files).status_code == 201
    index, chunk = _seam_chunk()

    body = client.post("/query", json={"question": chunk, "top_k": 1}).json()

    scored = [s["chunk_index"] for s in body["sources"] if not s["context_only"]]
    assert scored == [index], "precondition: the retrieved chunk is the one at the seam"
    return index, recorder.context


def _heading_above(text: str, needle: str) -> str:
    """The closest ``###`` heading line above the first line that contains ``needle``."""
    lines = text.split("\n")
    position = next(i for i, line in enumerate(lines) if needle in line)
    return next(line for line in reversed(lines[:position]) if line.startswith("### "))


def test_a_bullet_at_the_end_of_a_chunk_is_read_under_its_own_companys_heading(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _, context = _context_for_the_seam_chunk(app_with_database, tenant_with_key, SETTINGS)

    [excerpt] = context
    assert _heading_above(excerpt, BULLET).startswith("### Tessellate Systems")


def test_the_next_companys_heading_comes_after_the_bullet_never_before_it(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    _, context = _context_for_the_seam_chunk(app_with_database, tenant_with_key, SETTINGS)

    [excerpt] = context
    assert excerpt.index(BULLET) < excerpt.index("### Corvid Analytics")
    assert excerpt.count(BULLET) == 1, "the overlap between the two chunks is written once"


def test_the_excerpt_is_labelled_with_the_file_and_the_chunks_it_joins(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    index, context = _context_for_the_seam_chunk(app_with_database, tenant_with_key, SETTINGS)

    [excerpt] = context
    assert re.match(rf"\[cv\.md, chunks {index - 1}-{index}\]", excerpt)
