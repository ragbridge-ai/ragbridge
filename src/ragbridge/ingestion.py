"""Turn a document's raw bytes into stored, embedded chunks.

Split into two functions with different failure semantics, on purpose:
``parse_pages`` can fail on bad input (a corrupt PDF, non-UTF-8 text),
and the two callers - the synchronous upload path and the background
worker - need to react to that failure differently (an immediate HTTP
error with no row ever created, versus a document already sitting in
the database that must be marked ``failed``). ``ingest_document`` never
raises on bad *content* - by the time it runs, parsing has already
succeeded - so it is the part safe to share unconditionally between
both callers (see step 3, docs/plans/phase-3.md).
"""

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.cache import Cache
from ragbridge.chunking import chunk_text
from ragbridge.config import Settings
from ragbridge.db.models import Chunk, Document
from ragbridge.embeddings import Embedder
from ragbridge.pdf import PdfExtraction, extract_pdf_pages


def parse_pages(raw: bytes, content_type: str, pdf_extraction: PdfExtraction = "auto") -> list[str]:
    """Extract one string per page from raw uploaded bytes.

    ``pdf_extraction`` is ``Settings.pdf_extraction``; it only matters for PDFs.

    Raises ``ValueError`` for a malformed PDF (see ``extract_pdf_pages``)
    or ``UnicodeDecodeError`` for text that is not valid UTF-8.
    """
    if content_type == "application/pdf":
        return extract_pdf_pages(raw, pdf_extraction)
    return [raw.decode("utf-8")]


async def build_chunks(
    document: Document, pages: list[str], settings: Settings, embedder: Embedder
) -> list[Chunk]:
    """Chunk and embed ``pages``, returning ``Chunk`` objects for ``document``.

    Touches no database: embedding is the slow part, and keeping it apart from
    storing lets the background worker do it without holding a row lock (see
    ``store_chunks``).
    """
    is_pdf = document.content_type == "application/pdf"
    chunk_contents: list[str] = []
    chunk_metadata: list[dict[str, int]] = []
    for page_number, page_text in enumerate(pages, start=1):
        for chunk_content in chunk_text(
            page_text,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            min_size=settings.chunk_min_size,
        ):
            chunk_contents.append(chunk_content)
            chunk_metadata.append({"page": page_number} if is_pdf else {})

    embeddings = await embedder.embed(chunk_contents) if chunk_contents else []
    return [
        Chunk(
            document_id=document.id,
            tenant_id=document.tenant_id,
            chunk_index=index,
            content=content,
            embedding=embedding,
            metadata_=metadata,
        )
        for index, (content, metadata, embedding) in enumerate(
            zip(chunk_contents, chunk_metadata, embeddings, strict=True)
        )
    ]


async def store_chunks(
    session: AsyncSession,
    document: Document,
    pages: list[str],
    chunks: list[Chunk],
    cache: Cache,
    *,
    replace: bool = False,
) -> None:
    """Add ``chunks`` to ``document`` and mark it ready.

    With ``replace``, the document's existing chunks are deleted first, in the
    same transaction: a search sees the old chunks or the new ones, never a
    mixture and never none (decision 9, docs/plans/external-ids.md).
    """
    if replace:
        await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    session.add_all(chunks)
    document.content = "\n\n".join(pages)
    document.status = "ready"
    await cache.incr(f"corpus_version:{document.tenant_id}")


async def ingest_document(
    session: AsyncSession,
    document: Document,
    pages: list[str],
    settings: Settings,
    embedder: Embedder,
    cache: Cache,
) -> None:
    """Chunk and embed already-parsed ``pages``, storing them against ``document``.

    Sets ``document.content`` and ``document.status = "ready"`` on
    success, and bumps ``document.tenant_id``'s corpus version - new
    chunks become searchable in ``store_chunks``, which both the
    synchronous paths (through here) and the background worker call, which
    is what makes it the right place to invalidate the tenant's answer cache
    (decision 7, docs/plans/phase-3.md), not the moment a large upload
    is merely *accepted* as ``"pending"``. Never called with a page list
    that failed to parse - see the module docstring for why that split
    matters.
    """
    chunks = await build_chunks(document, pages, settings, embedder)
    await store_chunks(session, document, pages, chunks, cache)
