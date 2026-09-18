"""Endpoints for uploading and managing documents."""

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.auth import get_tenant
from ragbridge.chunking import chunk_text
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Chunk, Document
from ragbridge.db.session import get_session
from ragbridge.embeddings import Embedder, get_embedder
from ragbridge.pdf import extract_pdf_pages

router = APIRouter(prefix="/documents", tags=["documents"], dependencies=[Depends(get_tenant)])

ALLOWED_CONTENT_TYPES = {"text/plain", "text/markdown", "application/pdf"}


class DocumentOut(BaseModel):
    """Public representation of a stored document (raw content left out)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    created_at: datetime


@router.post("", response_model=DocumentOut)
async def upload_document(
    response: Response,
    file: UploadFile,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
) -> Document:
    """Upload a text, Markdown, or PDF document.

    The content is parsed, split into chunks, and embedded before it is
    stored. Uploading the same content twice is not an error: the second
    upload returns the existing document instead of redoing that work.
    """
    filename = file.filename
    content_type = file.content_type
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"unsupported content type: {content_type}",
        )
    if filename is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="filename is required"
        )

    raw = await file.read()
    if len(raw) > settings.max_upload_size:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="file too large")

    sha256 = hashlib.sha256(raw).hexdigest()
    existing = await session.scalar(select(Document).where(Document.sha256 == sha256))
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return existing

    if content_type == "application/pdf":
        try:
            pages = extract_pdf_pages(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
    else:
        try:
            pages = [raw.decode("utf-8")]
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="file is not valid UTF-8 text",
            ) from exc

    document = Document(
        filename=filename,
        content_type=content_type,
        sha256=sha256,
        content="\n\n".join(pages),
    )
    session.add(document)
    await session.flush()

    is_pdf = content_type == "application/pdf"
    chunk_contents: list[str] = []
    chunk_metadata: list[dict[str, int]] = []
    for page_number, page_text in enumerate(pages, start=1):
        for chunk_content in chunk_text(
            page_text, chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap
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
                chunk_index=index,
                content=content,
                embedding=embedding,
                metadata_=metadata,
            )
        )

    await session.commit()
    await session.refresh(document)
    response.status_code = status.HTTP_201_CREATED
    return document


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Sequence[Document]:
    """List all documents, newest first."""
    result = await session.scalars(select(Document).order_by(Document.created_at.desc()))
    return result.all()


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Delete a document by id."""
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")

    await session.delete(document)
    await session.commit()
