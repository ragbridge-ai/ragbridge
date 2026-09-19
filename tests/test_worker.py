"""Tests for process_document.

Called directly against a hand-built JobContext - no Redis, no real
worker process, matching how ragbridge.jobs.FakeJobQueue runs it in
production code too (see its docstring for why).
"""

import asyncio
import uuid

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.cache import FakeCache
from ragbridge.config import Settings
from ragbridge.db.models import Document, Tenant
from ragbridge.embeddings import FakeEmbedder
from ragbridge.worker import JobContext, WorkerSettings, process_document
from tests.helpers import fetch_chunks
from tests.pdf_fixtures import build_two_column_cv_pdf


async def _create_pending_document(
    session_factory: async_sessionmaker[AsyncSession], *, raw_content: bytes, content_type: str
) -> uuid.UUID:
    async with session_factory() as session:
        tenant = Tenant(name="acme")
        session.add(tenant)
        await session.flush()
        document = Document(
            tenant_id=tenant.id,
            filename="upload",
            content_type=content_type,
            sha256="deadbeef",
            raw_content=raw_content,
            status="pending",
        )
        session.add(document)
        await session.commit()
        return document.id


def _ctx(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> JobContext:
    return {
        "session_factory": session_factory,
        "embedder": FakeEmbedder(settings.embedding_dimension),
        "cache": FakeCache(),
        "settings": settings,
    }


async def _fetch_document(
    session_factory: async_sessionmaker[AsyncSession], document_id: uuid.UUID
) -> Document:
    async with session_factory() as session:
        result = await session.execute(select(Document).where(Document.id == document_id))
        return result.scalar_one()


def test_process_document_ingests_a_pending_text_document(app_with_database: FastAPI) -> None:
    settings = Settings()
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def run() -> Document:
        document_id = await _create_pending_document(
            session_factory, raw_content=b"Hello, ragbridge.", content_type="text/plain"
        )
        await process_document(_ctx(session_factory, settings), str(document_id))
        return await _fetch_document(session_factory, document_id)

    document = asyncio.run(run())

    assert document.status == "ready"
    assert document.content == "Hello, ragbridge."
    assert document.raw_content is None
    assert document.error is None


def test_process_document_stores_chunks(app_with_database: FastAPI) -> None:
    settings = Settings()
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def run() -> uuid.UUID:
        document_id = await _create_pending_document(
            session_factory,
            raw_content=b"First paragraph.\n\nSecond paragraph.",
            content_type="text/plain",
        )
        await process_document(_ctx(session_factory, settings), str(document_id))
        return document_id

    document_id = asyncio.run(run())
    chunks = asyncio.run(fetch_chunks(session_factory, document_id))

    assert [chunk.content for chunk in chunks] == ["First paragraph.", "Second paragraph."]


def test_process_document_records_failure_for_a_corrupt_pdf(app_with_database: FastAPI) -> None:
    settings = Settings()
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def run() -> Document:
        document_id = await _create_pending_document(
            session_factory, raw_content=b"not a pdf", content_type="application/pdf"
        )
        await process_document(_ctx(session_factory, settings), str(document_id))
        return await _fetch_document(session_factory, document_id)

    document = asyncio.run(run())

    assert document.status == "failed"
    assert document.error
    assert document.content is None


def test_worker_heartbeat_is_frequent_enough_for_a_container_healthcheck() -> None:
    """arq's default heartbeat is 3600s, so a crashed worker would report
    healthy for up to an hour. Measured against a SIGKILLed worker in the
    production stack: 30s detects the crash in about 35s.
    """
    assert WorkerSettings.health_check_interval <= 60


def test_process_document_reads_a_pdf_with_the_configured_extraction(
    app_with_database: FastAPI,
) -> None:
    """The background worker parses large PDFs, so it must use the same setting."""
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    async def run(settings: Settings) -> str:
        document_id = await _create_pending_document(
            session_factory,
            raw_content=build_two_column_cv_pdf("main_first"),
            content_type="application/pdf",
        )
        await process_document(_ctx(session_factory, settings), str(document_id))
        return "\n".join(c.content for c in await fetch_chunks(session_factory, document_id))

    row = "Mar 2022 - present Company A - Senior Engineer"
    assert row in asyncio.run(run(Settings()))
    assert row not in asyncio.run(run(Settings(pdf_extraction="plain")))
