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

from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.cache import Cache
from ragbridge.chunking import chunk_text
from ragbridge.config import Settings
from ragbridge.db.models import Chunk, Document
from ragbridge.embeddings import Embedder
from ragbridge.pdf import extract_pdf_pages


def parse_pages(raw: bytes, content_type: str) -> list[str]:
    """Extract one string per page from raw uploaded bytes.

    Raises ``ValueError`` for a malformed PDF (see ``extract_pdf_pages``)
    or ``UnicodeDecodeError`` for text that is not valid UTF-8.
    """
    if content_type == "application/pdf":
        return extract_pdf_pages(raw)
    return [raw.decode("utf-8")]


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
    success, and bumps ``document.tenant_id``'s corpus version - this is
    the one place new chunks actually become searchable for both the
    synchronous upload path and the background worker, which is what
    makes it the right place to invalidate the tenant's answer cache
    (decision 7, docs/plans/phase-3.md), not the moment a large upload
    is merely *accepted* as ``"pending"``. Never called with a page list
    that failed to parse - see the module docstring for why that split
    matters.
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
    for index, (content, metadata, embedding) in enumerate(
        zip(chunk_contents, chunk_metadata, embeddings, strict=True)
    ):
        session.add(
            Chunk(
                document_id=document.id,
                tenant_id=document.tenant_id,
                chunk_index=index,
                content=content,
                embedding=embedding,
                metadata_=metadata,
            )
        )

    document.content = "\n\n".join(pages)
    document.status = "ready"
    await cache.incr(f"corpus_version:{document.tenant_id}")
