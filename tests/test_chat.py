"""Unit tests for the fake chatter. No network calls, no database needed."""

import asyncio

from ragbridge.chat import FakeChatter


def test_fake_chatter_reports_how_many_chunks_it_used() -> None:
    chatter = FakeChatter()

    result = asyncio.run(chatter.answer("What is ragbridge?", ["chunk one", "chunk two"]))

    assert result == "Fake answer using 2 chunk(s)."


def test_fake_chatter_handles_empty_context() -> None:
    chatter = FakeChatter()

    result = asyncio.run(chatter.answer("What is ragbridge?", []))

    assert result == "Fake answer using 0 chunk(s)."
