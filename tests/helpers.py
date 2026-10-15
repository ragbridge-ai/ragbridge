"""Shared test helpers."""

import uuid
from collections.abc import Sequence

from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.auth import api_key_prefix, generate_api_key, hash_api_key
from ragbridge.config import get_settings
from ragbridge.db.models import ApiKey, Chunk, Document, Tenant
from ragbridge.embeddings import FakeEmbedder


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


async def tenant_id_for_key(
    session_factory: async_sessionmaker[AsyncSession], key: str
) -> uuid.UUID:
    """The tenant behind a raw API key, for tests that write to the database directly."""
    async with session_factory() as session:
        api_key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(key)))
        assert api_key is not None
        return api_key.tenant_id


async def new_tenant(session: AsyncSession) -> uuid.UUID:
    tenant = Tenant(name="acme")
    session.add(tenant)
    await session.flush()
    return tenant.id


async def add_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    texts: list[str],
    groups: list[str],
    *,
    chunk_groups: list[list[str]] | None = None,
) -> Document:
    """A document and its chunks, written straight to the database.

    ``chunk_groups`` overrides the groups of each chunk, to build a document whose chunks
    disagree with their document (the filter is on the chunks, so it must hold then too).
    """
    document = Document(
        tenant_id=tenant_id,
        filename=name,
        content_type="text/plain",
        sha256=name,
        access_groups=groups,
        status="ready",
    )
    session.add(document)
    await session.flush()
    embeddings = await FakeEmbedder(get_settings().embedding_dimension).embed(texts)
    session.add_all(
        Chunk(
            document_id=document.id,
            tenant_id=tenant_id,
            chunk_index=index,
            content=text,
            embedding=embedding,
            access_groups=chunk_groups[index] if chunk_groups else groups,
        )
        for index, (text, embedding) in enumerate(zip(texts, embeddings, strict=True))
    )
    await session.flush()
    return document


async def create_api_key(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    requires_access_scope: bool = False,
) -> str:
    """Another key for an existing tenant, returning the raw key."""
    key = generate_api_key()
    async with session_factory() as session:
        session.add(
            ApiKey(
                tenant_id=tenant_id,
                key_hash=hash_api_key(key),
                prefix=api_key_prefix(key),
                name="scoped" if requires_access_scope else "plain",
                requires_access_scope=requires_access_scope,
            )
        )
        await session.commit()
    return key
