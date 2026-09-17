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

from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Document
from ragbridge.db.session import get_session

router = APIRouter(prefix="/documents", tags=["documents"])

ALLOWED_CONTENT_TYPES = {"text/plain", "text/markdown"}


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
) -> Document:
    """Upload a text or Markdown document.

    Uploading the same content twice is not an error: the second upload
    returns the existing document instead of creating a duplicate.
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

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="file is not valid UTF-8 text",
        ) from exc

    sha256 = hashlib.sha256(raw).hexdigest()
    existing = await session.scalar(select(Document).where(Document.sha256 == sha256))
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return existing

    document = Document(filename=filename, content_type=content_type, sha256=sha256, content=text)
    session.add(document)
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
