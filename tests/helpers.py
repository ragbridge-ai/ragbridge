"""Shared test helpers."""

import uuid
from collections.abc import Sequence

from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.db.models import Chunk


def build_pdf(pages: list[str]) -> bytes:
    """Build a real PDF with one page per string, using fpdf2.

    Reading a PDF actually produced by a PDF library is a better test
    than hand-written PDF byte literals, which are brittle and hard to
    read in a diff.
    """
    pdf = FPDF()
    for text in pages:
        pdf.add_page()
        if text:
            pdf.set_font("Helvetica", size=12)
            pdf.cell(text=text)
    return bytes(pdf.output())


async def fetch_chunks(
    session_factory: async_sessionmaker[AsyncSession], document_id: uuid.UUID
) -> Sequence[Chunk]:
    """Fetch a document's chunks, ordered by position."""
    async with session_factory() as session:
        result = await session.scalars(
            select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.chunk_index)
        )
        return result.all()
