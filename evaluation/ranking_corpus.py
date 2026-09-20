"""A larger synthetic corpus for measuring ranking: many generic chunks, a few fact chunks.

The Acme corpus in ``corpus/`` has six documents of about one chunk each, so one
search returns nearly all of it and no ranking change can show. This one has about
a hundred chunks: most are *generic* "about the project" paragraphs that keep
repeating a fictional product name, and a handful are *fact* chunks a question can
actually be answered from. Ranking is measured by how high the fact chunk comes.

The long table chunk is the technology table of this repository's own
``AGENTS.md``, copied verbatim (``ranking/tech-stack-table.md``): a real, public,
19-row table, which embeds differently from prose. Everything else is invented.
"""

import random
from pathlib import Path

PRODUCT = "Zenvora"
TABLE_PATH = Path(__file__).parent / "ranking" / "tech-stack-table.md"

FACTS = {
    "license": (
        f"Licensing: {PRODUCT} is released under the MIT license. Anyone may use, copy and "
        "change the code, and the license text ships with every release."
    ),
    "commands": (
        "Running the checks: run the tests with `uv run pytest`, lint with `uv run ruff check .` "
        "and check types with `uv run mypy`. All three must pass before a change is merged."
    ),
    "release": (
        "Release history: version 2.0 was released in March 2025 and dropped support for "
        "Python 3.10; version 1.4 added the reporting API and version 1.0 was the first "
        "stable release."
    ),
    "support": (
        "Getting help: questions are answered on the community forum, and security reports go "
        "to the maintainers by email so that they are not made public before a fix is ready."
    ),
}

# Chunks that talk *about* the same words a question uses, without answering it.
DISTRACTORS = [
    f"Talks and workshops: the {PRODUCT} community hosts talks about web framework design, "
    f"database tuning and package tooling, and the {PRODUCT} project publishes every recording.",
    f"Comparing tools: before starting, the {PRODUCT} team read many articles that compare a "
    "web framework with another one, but those notes are opinions and not part of the project.",
    f"Newsletter: each month the {PRODUCT} newsletter lists which license changes, database "
    f"releases and framework news the {PRODUCT} project followed during the month.",
    f"Reading list: the {PRODUCT} project keeps a reading list about databases, type checking "
    "and testing, chosen by volunteers who enjoy explaining these topics to newcomers.",
]

_TOPICS = [
    "community",
    "governance",
    "roadmap",
    "philosophy",
    "events",
    "sponsors",
    "tutorials",
    "translations",
    "mentoring",
    "accessibility",
    "history",
    "principles",
    "gallery",
    "press",
    "partners",
    "volunteers",
    "workshops",
    "stories",
    "feedback",
    "planning",
    "teams",
    "values",
    "handbook",
    "glossary",
    "showcase",
    "meetups",
    "awards",
    "interviews",
    "reviews",
    "ideas",
    "diary",
    "notes",
    "vision",
    "mission",
    "culture",
    "welcome",
    "tips",
    "guides",
    "stewards",
    "champions",
    "labs",
    "studio",
    "garden",
    "harbour",
    "bridge",
    "compass",
    "lantern",
    "meadow",
    "orchard",
    "summit",
    "valley",
    "workshop",
    "library",
    "kitchen",
    "market",
    "station",
    "theatre",
    "gallery walk",
    "open days",
    "study groups",
    "office hours",
    "hack nights",
]
_QUALITIES = ["simple", "friendly", "dependable", "welcoming", "open", "calm", "clear", "kind"]
_AUDIENCES = ["newcomers", "long-time users", "small teams", "students", "volunteers", "readers"]
_CADENCES = ["every week", "every month", "each season", "once a quarter", "every few days"]


def _generic(rng: random.Random, topic: str) -> str:
    quality, audience, cadence = (
        rng.choice(_QUALITIES),
        rng.choice(_AUDIENCES),
        rng.choice(_CADENCES),
    )
    return (
        f"{PRODUCT} {topic}: {PRODUCT} is built for {audience}, and the {PRODUCT} project keeps "
        f"its {topic} {quality}. Whether you are new to {PRODUCT} or a long-time member of the "
        f"{PRODUCT} project, the {PRODUCT} team shares {topic} news {cadence}."
    )


def build_documents(generic_chunks: int = 91, seed: int = 11) -> dict[str, str]:
    """Filename -> text. About a hundred chunks: the table, four facts, and the rest generic.

    ``generic_chunks`` counts the plain generic paragraphs; the distractors are extra.
    Each paragraph is longer than the default ``CHUNK_MIN_SIZE`` and separated by a blank
    line, so each becomes a chunk of its own.
    """
    rng = random.Random(seed)
    topics = [f"{topic} page" for topic in _TOPICS]
    while len(topics) < generic_chunks:
        topics.append(f"{rng.choice(_TOPICS)} corner {len(topics)}")
    paragraphs = [_generic(rng, topics[i]) for i in range(generic_chunks)]
    rng.shuffle(paragraphs)

    documents = {
        "tech-stack.md": TABLE_PATH.read_text(),
        "facts.md": "\n\n".join(FACTS.values()) + "\n",
        "distractors.md": "\n\n".join(DISTRACTORS) + "\n",
    }
    per_document = 23
    for start in range(0, len(paragraphs), per_document):
        name = f"about-{start // per_document + 1}.md"
        documents[name] = "\n\n".join(paragraphs[start : start + per_document]) + "\n"
    return documents
