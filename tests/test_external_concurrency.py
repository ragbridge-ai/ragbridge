"""Two writers of one external id at the same time, against the real test database.

These call ``put_external_document`` from separate sessions with ``asyncio.gather``
instead of going through the HTTP client, which is synchronous. The embedder sleeps,
so the first writer is still inside its transaction (row inserted or locked, not yet
committed) when the second one starts: the overlap is real, not hoped for.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.cache import FakeCache
from ragbridge.config import Settings
from ragbridge.db.models import Chunk, Document, Tenant
from ragbridge.embeddings import FakeEmbedder
from ragbridge.sync import SyncOutcome, SyncPayload, _lock_existing, put_external_document

SOURCE_TIME = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)


class SlowEmbedder:
    """Embeds like ``FakeEmbedder`` after a pause, to keep a transaction open."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._inner = FakeEmbedder(Settings().embedding_dimension)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(self._delay)
        return await self._inner.embed(texts)


class NoQueue:
    async def enqueue_process_document(
        self, document_id: uuid.UUID, sha256: str | None = None
    ) -> None:
        raise AssertionError("nothing in these tests is large enough to be queued")


def _payload(content: str, minutes: int | None = None) -> SyncPayload:
    when = None if minutes is None else SOURCE_TIME + timedelta(minutes=minutes)
    return SyncPayload(title=content, content=content, metadata={}, source_updated_at=when)


async def _tenant(app: FastAPI) -> uuid.UUID:
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as session:
        tenant = Tenant(name="acme")
        session.add(tenant)
        await session.commit()
        return tenant.id


def _writer(
    app: FastAPI, tenant_id: uuid.UUID, payload: SyncPayload, delay: float
) -> Callable[[], Awaitable[SyncOutcome]]:
    async def write() -> SyncOutcome:
        session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
        async with session_factory() as session:
            return await put_external_document(
                session,
                tenant_id,
                "post:1",
                payload,
                Settings(),
                SlowEmbedder(delay),
                FakeCache(),
                NoQueue(),
            )

    return write


async def _after(seconds: float, write: Callable[[], Awaitable[SyncOutcome]]) -> SyncOutcome:
    await asyncio.sleep(seconds)
    return await write()


async def _documents_and_chunks(app: FastAPI) -> tuple[list[Document], list[Chunk]]:
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as session:
        documents = list((await session.scalars(select(Document))).all())
        chunks = list((await session.scalars(select(Chunk).order_by(Chunk.chunk_index))).all())
        return documents, chunks


def test_two_simultaneous_puts_of_a_new_id_create_one_document(
    app_with_database: FastAPI,
) -> None:
    async def run() -> tuple[list[SyncOutcome], list[Document], list[Chunk]]:
        tenant_id = await _tenant(app_with_database)
        payload = _payload("The same record, sent twice at once.")
        write = _writer(app_with_database, tenant_id, payload, delay=0.4)
        outcomes = await asyncio.gather(write(), _after(0.1, write))
        documents, chunks = await _documents_and_chunks(app_with_database)
        return list(outcomes), documents, chunks

    outcomes, documents, chunks = asyncio.run(run())

    assert sorted(outcome.result for outcome in outcomes) == ["created", "unchanged"]
    assert len(documents) == 1
    assert len(chunks) == 1


def test_many_simultaneous_puts_of_a_new_id_still_create_one_document(
    app_with_database: FastAPI,
) -> None:
    async def run() -> tuple[list[SyncOutcome], list[Document]]:
        tenant_id = await _tenant(app_with_database)
        writers = [
            _writer(app_with_database, tenant_id, _payload(f"Version {n}."), delay=0.2)
            for n in range(5)
        ]
        outcomes = await asyncio.gather(*(_after(n * 0.02, w) for n, w in enumerate(writers)))
        documents, _ = await _documents_and_chunks(app_with_database)
        return list(outcomes), documents

    outcomes, documents = asyncio.run(run())

    assert [outcome.result for outcome in outcomes].count("created") == 1
    assert len(documents) == 1


def test_simultaneous_writes_of_different_text_leave_chunks_that_match_the_document(
    app_with_database: FastAPI,
) -> None:
    async def run() -> tuple[list[Document], list[Chunk]]:
        tenant_id = await _tenant(app_with_database)
        await _writer(app_with_database, tenant_id, _payload("Original text."), delay=0)()
        first = _writer(app_with_database, tenant_id, _payload("Text from writer one."), 0.3)
        second = _writer(app_with_database, tenant_id, _payload("Text from writer two."), 0.3)
        await asyncio.gather(first(), _after(0.1, second))
        return await _documents_and_chunks(app_with_database)

    documents, chunks = asyncio.run(run())

    assert len(documents) == 1
    assert [chunk.content for chunk in chunks] == [documents[0].content]
    assert documents[0].content in {"Text from writer one.", "Text from writer two."}


def test_a_stale_write_that_arrives_while_a_newer_one_is_being_saved_is_still_ignored(
    app_with_database: FastAPI,
) -> None:
    """The ordering check runs under the row lock, so it cannot be raced past."""

    async def run() -> tuple[SyncOutcome, SyncOutcome, list[Document]]:
        tenant_id = await _tenant(app_with_database)
        await _writer(app_with_database, tenant_id, _payload("Original.", minutes=0), 0)()
        newer = _writer(app_with_database, tenant_id, _payload("Newer.", minutes=5), delay=0.4)
        older = _writer(app_with_database, tenant_id, _payload("Older.", minutes=3), delay=0)
        first, second = await asyncio.gather(newer(), _after(0.1, older))
        documents, _ = await _documents_and_chunks(app_with_database)
        return first, second, documents

    newer, older, documents = asyncio.run(run())

    assert newer.result == "replaced"
    assert older.result == "stale"
    assert documents[0].content == "Newer."
    assert documents[0].source_updated_at == SOURCE_TIME + timedelta(minutes=5)


def test_writes_that_arrive_in_the_wrong_order_end_with_the_newest_one(
    app_with_database: FastAPI,
) -> None:
    async def run() -> list[Document]:
        tenant_id = await _tenant(app_with_database)
        await _writer(app_with_database, tenant_id, _payload("Original.", minutes=0), 0)()
        older = _writer(app_with_database, tenant_id, _payload("Older.", minutes=3), delay=0.4)
        newer = _writer(app_with_database, tenant_id, _payload("Newer.", minutes=5), delay=0)
        await asyncio.gather(older(), _after(0.1, newer))
        documents, _ = await _documents_and_chunks(app_with_database)
        return documents

    documents = asyncio.run(run())

    assert documents[0].content == "Newer."
    assert documents[0].source_updated_at == SOURCE_TIME + timedelta(minutes=5)


def test_writes_to_different_ids_do_not_wait_for_each_other(app_with_database: FastAPI) -> None:
    async def run() -> float:
        tenant_id = await _tenant(app_with_database)

        def writer_for(external_id: str) -> Callable[[], Awaitable[SyncOutcome]]:
            async def write() -> SyncOutcome:
                session_factory: async_sessionmaker[AsyncSession] = (
                    app_with_database.state.session_factory
                )
                async with session_factory() as session:
                    return await put_external_document(
                        session,
                        tenant_id,
                        external_id,
                        _payload(f"Record {external_id}."),
                        Settings(),
                        SlowEmbedder(0.5),
                        FakeCache(),
                        NoQueue(),
                    )

            return write

        started = asyncio.get_running_loop().time()
        await asyncio.gather(*(writer_for(f"post:{n}")() for n in range(4)))
        return asyncio.get_running_loop().time() - started

    elapsed = asyncio.run(run())

    assert elapsed < 1.5  # four 0.5 s embeddings, in parallel and not one after another


def test_only_one_row_exists_per_tenant_and_id_after_all_of_it(app_with_database: FastAPI) -> None:
    async def run() -> int:
        tenant_id = await _tenant(app_with_database)
        writers = [
            _writer(app_with_database, tenant_id, _payload(f"Text {n}."), delay=0.1)
            for n in range(4)
        ]
        await asyncio.gather(*(w() for w in writers))
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(Document).where(Document.external_id == "post:1")
            )
            return count or 0

    assert asyncio.run(run()) == 1


def test_a_writer_holding_only_the_row_lock_makes_the_next_writer_wait_and_then_see_its_result(
    app_with_database: FastAPI,
) -> None:
    """The lock on its own, with nothing modified yet to make anyone wait.

    Writer one takes the row lock, thinks for a while, then saves a newer record.
    Writer two arrives meanwhile with an older one. Without the lock it would read the
    old row at once and apply its write; with it, it waits, then sees the newer time and
    is told ``stale``.
    """

    async def run() -> SyncOutcome:
        tenant_id = await _tenant(app_with_database)
        await _writer(app_with_database, tenant_id, _payload("Original.", minutes=0), 0)()
        session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

        async def hold_the_lock_then_save_a_newer_record() -> None:
            async with session_factory() as session:
                document = await _lock_existing(session, tenant_id, "post:1")
                assert document is not None
                await asyncio.sleep(0.5)
                document.source_updated_at = SOURCE_TIME + timedelta(minutes=5)
                document.filename = "Saved while holding the lock"
                await session.commit()

        older = _writer(app_with_database, tenant_id, _payload("Older.", minutes=3), delay=0)
        _, second = await asyncio.gather(
            hold_the_lock_then_save_a_newer_record(), _after(0.1, older)
        )
        return second

    assert asyncio.run(run()).result == "stale"
