"""Endpoints for uploading and managing documents."""

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Response, UploadFile, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.auth import get_tenant
from ragbridge.cache import Cache, get_cache
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Document, Tenant
from ragbridge.db.session import get_session
from ragbridge.embeddings import Embedder, get_embedder
from ragbridge.ingestion import ingest_document, parse_pages
from ragbridge.jobs import JobQueue, get_job_queue
from ragbridge.sync import ExternalIdTaken, SyncPayload, SyncResult, put_external_document

router = APIRouter(prefix="/documents", tags=["documents"])

ALLOWED_CONTENT_TYPES = {"text/plain", "text/markdown", "application/pdf"}

EXTERNAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$"
ExternalId = Annotated[
    str,
    Path(
        pattern=EXTERNAL_ID_PATTERN,
        description=(
            "Your application's id for the record: 1-255 ASCII letters, digits or "
            "`. _ : @ -`, starting with a letter or digit. No `/`."
        ),
        examples=["post:42"],
    ),
]
"""The client application's id for a record (decision 3, docs/plans/external-ids.md).

No ``/``: an encoded slash is decoded before routing, so the id's meaning would
depend on every proxy in between. Starting with a letter or digit rules out ``.``
and ``..``. ASCII only, so two ids that look alike cannot differ by Unicode form.
"""


class DocumentOut(BaseModel):
    """Public representation of a stored document (raw content left out)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_id: str | None
    """The client application's id for this record; ``None`` for an upload."""
    filename: str
    content_type: str
    status: Literal["pending", "processing", "ready", "failed"]
    error: str | None
    # The column is ``metadata_`` because ``metadata`` is SQLAlchemy's own attribute.
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    source_updated_at: datetime | None
    created_at: datetime
    updated_at: datetime


MAX_METADATA_BYTES = 16 * 1024


class ExternalDocumentIn(BaseModel):
    """The current state of one record in the client application."""

    title: str = Field(min_length=1, max_length=500)
    """Shown as the document's name in sources; stored as ``filename``."""
    content: str
    """The record's text. At least one character that is not whitespace."""
    metadata: dict[str, Any] = Field(default_factory=dict)
    """Free-form JSON object, at most 16 KB. Returned as sent; not searched yet."""
    source_updated_at: AwareDatetime | None = None
    """When the record last changed in your application, with a time zone."""

    @field_validator("content")
    @classmethod
    def _content_must_have_text(cls, content: str) -> str:
        if not content.strip():
            raise ValueError("content must contain text; delete the document to empty it")
        return content

    @field_validator("metadata")
    @classmethod
    def _metadata_must_be_small(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(metadata).encode()) > MAX_METADATA_BYTES:
            raise ValueError(f"metadata must be at most {MAX_METADATA_BYTES} bytes as JSON")
        return metadata


class ExternalDocumentResult(BaseModel):
    """What a ``PUT`` did, and the document as it now stands."""

    result: SyncResult
    document: DocumentOut


@router.post("", response_model=DocumentOut)
async def upload_document(
    response: Response,
    file: UploadFile,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
    settings: Annotated[Settings, Depends(get_settings)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    job_queue: Annotated[JobQueue, Depends(get_job_queue)],
    cache: Annotated[Cache, Depends(get_cache)],
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
    document (decision 3, docs/plans/phase-3.md). It is compared only
    with other uploads: a document a client keeps in sync by external id
    can hold the same text, and a later sync would change or delete it
    under the uploader (decision 2, docs/plans/external-ids.md).
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
        select(Document).where(
            Document.tenant_id == tenant.id,
            Document.sha256 == sha256,
            Document.external_id.is_(None),
        )
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
        pages = parse_pages(raw, content_type, settings.pdf_extraction)
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

    await ingest_document(session, document, pages, settings, embedder, cache)

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
    cache: Annotated[Cache, Depends(get_cache)],
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
    await cache.incr(f"corpus_version:{tenant.id}")


@router.get("/external/{external_id}", response_model=DocumentOut)
async def get_document_by_external_id(
    external_id: ExternalId,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> Document:
    """Fetch the document your application syncs under ``external_id``."""
    document = await _find_by_external_id(session, tenant, external_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    return document


async def _find_by_external_id(
    session: AsyncSession, tenant: Tenant, external_id: str
) -> Document | None:
    document: Document | None = await session.scalar(
        select(Document).where(Document.tenant_id == tenant.id, Document.external_id == external_id)
    )
    return document


@router.put("/external/{external_id}", response_model=ExternalDocumentResult)
async def put_document_by_external_id(
    external_id: ExternalId,
    body: ExternalDocumentIn,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant: Annotated[Tenant, Depends(get_tenant)],
    settings: Annotated[Settings, Depends(get_settings)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    cache: Annotated[Cache, Depends(get_cache)],
) -> ExternalDocumentResult:
    """Create or replace the document your application syncs under ``external_id``.

    Safe to repeat: sending the same record again changes nothing.
    """
    if len(body.content.encode()) > settings.max_upload_size:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="content too large"
        )

    payload = SyncPayload(body.title, body.content, body.metadata, body.source_updated_at)
    try:
        outcome = await put_external_document(
            session, tenant.id, external_id, payload, settings, embedder, cache
        )
    except ExternalIdTaken as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="replacing is not implemented yet"
        ) from exc

    if outcome.result == "created":
        response.status_code = status.HTTP_201_CREATED
    return ExternalDocumentResult(
        result=outcome.result, document=DocumentOut.model_validate(outcome.document)
    )
