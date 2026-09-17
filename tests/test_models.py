"""Tests for ORM models, against the real test database."""

import asyncio
import hashlib

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.db.models import Document


def test_document_round_trip(app_with_database: FastAPI) -> None:
    async def create_and_read_back() -> Document:
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        content = "Hello, ragbridge."

        async with session_factory() as session:
            document = Document(
                filename="hello.txt",
                content_type="text/plain",
                sha256=hashlib.sha256(content.encode()).hexdigest(),
                content=content,
            )
            session.add(document)
            await session.commit()
            document_id = document.id

        async with session_factory() as session:
            result = await session.execute(select(Document).where(Document.id == document_id))
            return result.scalar_one()

    stored = asyncio.run(create_and_read_back())

    assert stored.filename == "hello.txt"
    assert stored.content_type == "text/plain"
    assert stored.content == "Hello, ragbridge."
    assert stored.created_at is not None
