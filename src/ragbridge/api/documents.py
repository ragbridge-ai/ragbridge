"""Endpoints for uploading and managing documents."""

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.auth import get_tenant
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Document, Tenant
from ragbridge.db.session import get_session
from ragbridge.embeddings import Embedder, get_embedder
from ragbridge.ingestion import ingest_document, parse_pages
from ragbridge.jobs import JobQueue, get_job_queue

router = APIRouter(prefix="/documents", tags=["documents"])

ALLOWED_CONTENT_TYPES = {"text/plain", "text/markdown", "application/pdf"}


class DocumentOut(BaseModel):
    """Public representation of a stored document (raw content left out)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    status: Literal["pending", "processing", "ready", "failed"]
    error: str | None
    created_at: datetime


@router.post("", response_model=DocumentOut)
async def upload_document(
    response: Response,
    file: UploadFile,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
    settings: Annotated[Settings, Depends(get_settings)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    job_queue: Annotated[JobQueue, Depends(get_job_queue)],
) -> Document:
    """Upload a text, Markdown, or PDF document.

    Uploads over ``settings.async_processing_threshold`` are stored with
    ``status = "pending"`` and handed to the background worker instead
    of being parsed within the request - the response comes back `202`
    immediately, and a client polls ``GET /documents/{id}`` until
    ``status`` is no longer ``"pending"``/``"processing"`` (decision 6,
    docs/plans/phase-3.md). Smaller uploads are parsed, chunked, and
    embedded before the response, exactly as before, and come back `201`
    with ``status = "ready"``.

    Uploading the same content twice is not an error: the second upload
    returns the existing document instead of redoing that work - but
    only within the same tenant. sha256 is unique per tenant, not
    globally, so two tenants uploading the same file each get their own
    document (decision 3, docs/plans/phase-3.md).
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
    existing = await session.scalar(
        select(Document).where(Document.tenant_id == tenant.id, Document.sha256 == sha256)
    )
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return existing

    if len(raw) > settings.async_processing_threshold:
        document = Document(
            tenant_id=tenant.id,
            filename=filename,
            content_type=content_type,
            sha256=sha256,
            raw_content=raw,
            status="pending",
        )
        session.add(document)
        await session.commit()
        await job_queue.enqueue_process_document(document.id)
        response.status_code = status.HTTP_202_ACCEPTED
        return document

    try:
        pages = parse_pages(raw, content_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="file is not valid UTF-8 text",
        ) from exc

    document = Document(
        tenant_id=tenant.id,
        filename=filename,
        content_type=content_type,
        sha256=sha256,
    )
    session.add(document)
    await session.flush()

    await ingest_document(session, document, pages, settings, embedder)

    await session.commit()
    await session.refresh(document)
    response.status_code = status.HTTP_201_CREATED
    return document


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> Sequence[Document]:
    """List the calling tenant's documents, newest first."""
    result = await session.scalars(
        select(Document).where(Document.tenant_id == tenant.id).order_by(Document.created_at.desc())
    )
    return result.all()


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> Document:
    """Fetch one document by id - for a client to poll an async upload's status."""
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant.id)
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    return document


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> None:
    """Delete a document by id.

    404, not 403, when the document belongs to another tenant: a 403
    would confirm the id exists, which is itself information a caller
    should not get for data it cannot see.
    """
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.tenant_id == tenant.id)
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")

    await session.delete(document)
    await session.commit()
