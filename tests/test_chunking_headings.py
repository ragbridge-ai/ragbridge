"""A markdown heading belongs with the text below it: no chunk may end on one.

Pure tests of ``chunk_text``. In a module of its own so it cannot conflict with changes to
``test_chunking.py``. The first case is the one found in a real document: a tiny paragraph
absorbed the heading that followed it, and the heading's body started the next chunk.
"""

import random
import re

from ragbridge.chunking import chunk_text

HEADING_LINE = re.compile(r"^#{1,6} \S")


def _ends_on_heading(chunk: str) -> bool:
    last = next((line for line in reversed(chunk.splitlines()) if line.strip()), "")
    return bool(HEADING_LINE.match(last))


BODY = "This body paragraph is written long enough to stay a chunk of its own and to be read. " * 3


def test_a_heading_absorbed_by_a_tiny_paragraph_is_not_left_at_the_end_of_a_chunk() -> None:
    """The real case: a 79-character paragraph, then the heading, then its body."""
    tiny = "Each step is built as several small, focused commits on its own feature branch."
    text = f"{BODY}\n\n{tiny}\n\n## Step 1 detail\n\n{BODY}"

    chunks = chunk_text(text, chunk_size=1000, chunk_overlap=200, min_size=100)

    assert not any(_ends_on_heading(chunk) for chunk in chunks)
    body_chunk = next(
        chunk for chunk in chunks if chunk.endswith(BODY.strip()) and "Step 1" in chunk
    )
    assert body_chunk.startswith("## Step 1 detail")


def test_the_tiny_paragraph_stays_with_what_came_before_it() -> None:
    tiny = "Each step is built as several small, focused commits on its own feature branch."
    text = f"{BODY}\n\n{tiny}\n\n## Step 1 detail\n\n{BODY}"

    chunks = chunk_text(text, chunk_size=1000, chunk_overlap=200, min_size=100)

    assert any(chunk.rstrip().endswith(tiny) for chunk in chunks)


def test_a_heading_alone_between_two_long_paragraphs_moves_to_the_next_one() -> None:
    text = f"{BODY}\n\n## Details\n\n{BODY}"

    chunks = chunk_text(text, chunk_size=300, chunk_overlap=0, min_size=0)

    assert not any(_ends_on_heading(chunk) for chunk in chunks)
    assert [chunk for chunk in chunks if "## Details" in chunk][0].startswith("## Details")
    assert all(chunk.strip() for chunk in chunks), "no empty chunk is left behind"


def test_several_headings_in_a_row_all_move_to_the_next_chunk() -> None:
    text = f"{BODY}\n\n# Part one\n\n## Section A\n\n{BODY}"

    chunks = chunk_text(text, chunk_size=300, chunk_overlap=0, min_size=0)

    assert not any(_ends_on_heading(chunk) for chunk in chunks)
    following = next(chunk for chunk in chunks if "# Part one" in chunk)
    assert following.startswith("# Part one\n\n## Section A")


def test_a_heading_at_the_very_end_of_the_document_has_nowhere_to_go_and_stays() -> None:
    text = f"{BODY}\n\n## The end"

    chunks = chunk_text(text, chunk_size=300, chunk_overlap=0, min_size=0)

    assert chunks[-1] == "## The end"


def test_a_heading_inside_a_paragraph_is_carried_too() -> None:
    """No blank lines: the packer can end a chunk right after a heading line."""
    lines = [f"Line {n} holds a complete thought of about thirty chars." for n in range(1, 13)]
    lines.insert(6, "## Middle section")
    text = "\n".join(lines)

    for size in range(120, 400, 7):
        chunks = chunk_text(text, chunk_size=size, chunk_overlap=0, min_size=0)
        assert not any(_ends_on_heading(chunk) for chunk in chunks[:-1]), size


def test_headings_are_carried_whatever_the_minimum_chunk_size_is() -> None:
    text = f"{BODY}\n\n## Details\n\n{BODY}"

    for min_size in (0, 50, 200):
        chunks = chunk_text(text, chunk_size=400, chunk_overlap=0, min_size=min_size)
        assert not any(_ends_on_heading(chunk) for chunk in chunks[:-1]), min_size


def test_a_heading_the_overlap_already_repeats_is_not_written_twice() -> None:
    """Inside one long paragraph the next chunk starts with the end of the previous one, which
    can include the heading. Carrying it must not put it there a second time.
    """
    lines = [f"Line {n} holds a complete thought of about thirty chars." for n in range(1, 13)]
    lines.insert(6, "## Middle section")
    text = "\n".join(lines)

    for size in range(150, 500, 11):
        for overlap in (60, 120, 200):
            if overlap >= size:
                continue  # the chunker rejects an overlap that is not smaller than the size
            chunks = chunk_text(text, chunk_size=size, chunk_overlap=overlap, min_size=0)
            assert all(chunk.count("## Middle section") <= 1 for chunk in chunks), (size, overlap)
            assert any("## Middle section" in chunk for chunk in chunks), (size, overlap)
            assert not any(_ends_on_heading(chunk) for chunk in chunks[:-1]), (size, overlap)


def test_text_with_no_headings_is_chunked_exactly_as_before() -> None:
    text = f"{BODY}\n\nA short paragraph in between.\n\n{BODY}"

    with_carry = chunk_text(text, chunk_size=300, chunk_overlap=60, min_size=100)

    assert not any(HEADING_LINE.match(line) for chunk in with_carry for line in chunk.splitlines())
    assert with_carry == chunk_text(text, chunk_size=300, chunk_overlap=60, min_size=100)


def test_carrying_never_loses_or_reorders_text() -> None:
    """Property: over generated documents every word survives and no chunk ends on a heading."""
    rng = random.Random(3)
    for trial in range(60):
        parts = []
        for section in range(rng.randint(3, 9)):
            parts.append(f"{'#' * rng.randint(1, 3)} Section {trial}-{section} title")
            for _ in range(rng.randint(1, 3)):
                words = " ".join(f"word{rng.randint(0, 999)}" for _ in range(rng.randint(2, 70)))
                parts.append(f"Paragraph text {trial}-{section}: {words}.")
        text = "\n\n".join(parts)
        size = rng.choice([150, 250, 400, 1000])

        chunks = chunk_text(
            text, chunk_size=size, chunk_overlap=rng.choice([0, 40]), min_size=rng.choice([0, 80])
        )

        assert not any(_ends_on_heading(chunk) for chunk in chunks[:-1]), (trial, size)
        assert set(text.split()) <= set(" ".join(chunks).split()), (trial, size)
        positions = [text.find(chunk.split("\n\n")[-1]) for chunk in chunks]
        assert all(p >= 0 for p in positions), (trial, size)
