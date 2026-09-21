"""Tests for the endpoints that identify a document by the client's own id."""

import asyncio
import hashlib
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.cache import FakeCache, get_cache
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Chunk, Document, Tenant
from ragbridge.embeddings import FakeEmbedder, get_embedder
from ragbridge.jobs import get_job_queue
from ragbridge.sync import content_hash
from ragbridge.worker import JobContext, process_document
from tests.helpers import fetch_chunks


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


async def _insert_synced_document(
    app: FastAPI, tenant_name: str, external_id: str, content: str = "Some text."
) -> uuid.UUID:
    """Insert a synced document directly, until PUT exists to create one."""
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as session:
        tenant_id = (
            await session.execute(select(Tenant.id).where(Tenant.name == tenant_name))
        ).scalar_one()
        document = Document(
            tenant_id=tenant_id,
            external_id=external_id,
            filename="A title",
            content_type="text/plain",
            sha256=hashlib.sha256(content.encode()).hexdigest(),
            content=content,
            metadata_={"lang": "en"},
        )
        session.add(document)
        await session.commit()
        return document.id


def test_get_by_external_id_returns_the_document(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    document_id = asyncio.run(_insert_synced_document(app_with_database, "test-tenant", "post:42"))

    response = _client(app_with_database, tenant_with_key).get("/documents/external/post:42")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(document_id)
    assert body["external_id"] == "post:42"
    assert body["filename"] == "A title"
    assert body["metadata"] == {"lang": "en"}


def test_get_by_external_id_is_404_when_there_is_none(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    response = _client(app_with_database, tenant_with_key).get("/documents/external/post:42")

    assert response.status_code == 404


def test_another_tenants_external_id_is_404(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    asyncio.run(_insert_synced_document(app_with_database, "test-tenant", "post:42"))

    response = _client(app_with_database, second_tenant_with_key).get("/documents/external/post:42")

    assert response.status_code == 404


def test_the_same_external_id_in_two_tenants_is_two_documents(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    first = asyncio.run(_insert_synced_document(app_with_database, "test-tenant", "post:1"))
    second = asyncio.run(_insert_synced_document(app_with_database, "test-tenant-2", "post:1"))

    one = _client(app_with_database, tenant_with_key).get("/documents/external/post:1")
    two = _client(app_with_database, second_tenant_with_key).get("/documents/external/post:1")

    assert one.json()["id"] == str(first)
    assert two.json()["id"] == str(second)


def test_the_external_id_is_case_sensitive(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    asyncio.run(_insert_synced_document(app_with_database, "test-tenant", "Post:42"))

    response = _client(app_with_database, tenant_with_key).get("/documents/external/post:42")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "external_id",
    [
        "post:42",
        "post%3A42",
        "user@example.com",
        "shop-de:product:9f1c2b3a",
        "wp_posts.42",
        "7",
        "a" * 255,
    ],
)
def test_valid_external_ids_reach_the_handler(
    app_with_database: FastAPI, tenant_with_key: str, external_id: str
) -> None:
    response = _client(app_with_database, tenant_with_key).get(f"/documents/external/{external_id}")

    assert response.status_code == 404  # valid id, no such document


@pytest.mark.parametrize(
    "external_id",
    [
        "a" * 256,  # too long
        "%2E%2E",  # decodes to ".."
        ".hidden",  # must start with a letter or digit
        "-dash",
        "_under",
        "caf%C3%A9",  # not ASCII
        "a%20b",  # a space
        "a%2Fb",  # an encoded slash never reaches the id (see below)
        "a%3Fb",  # "?"
        "a%23b",  # "#"
    ],
)
def test_invalid_external_ids_are_rejected(
    app_with_database: FastAPI, tenant_with_key: str, external_id: str
) -> None:
    response = _client(app_with_database, tenant_with_key).get(f"/documents/external/{external_id}")

    assert response.status_code in {404, 422}
    assert response.status_code != 200


def test_an_id_outside_the_pattern_is_422_and_names_the_rule(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    response = _client(app_with_database, tenant_with_key).get("/documents/external/.hidden")

    assert response.status_code == 422
    assert "pattern" in response.text


def test_get_by_external_id_needs_authentication(app_with_database: FastAPI) -> None:
    response = TestClient(app_with_database).get("/documents/external/post:42")

    assert response.status_code == 401


def test_an_upload_still_works_beside_the_external_routes(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    created = client.post("/documents", files={"file": ("a.txt", b"Plain.", "text/plain")})

    assert client.get(f"/documents/{created.json()['id']}").status_code == 200


RECORD = {
    "title": "Refund policy",
    "content": "Refunds are possible within 14 days of purchase.",
    "metadata": {"lang": "en", "post_id": 42},
    "source_updated_at": "2026-09-21T10:00:00Z",
}


def _put(client: TestClient, external_id: str = "post:42", **changes: Any) -> Response:
    return client.put(f"/documents/external/{external_id}", json={**RECORD, **changes})


def test_put_creates_a_document_under_the_external_id(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    response = _put(client)

    assert response.status_code == 201
    body = response.json()
    assert body["result"] == "created"
    document = body["document"]
    assert document["external_id"] == "post:42"
    assert document["filename"] == "Refund policy"
    assert document["content_type"] == "text/plain"
    assert document["status"] == "ready"
    assert document["metadata"] == {"lang": "en", "post_id": 42}
    assert document["source_updated_at"] == "2026-09-21T10:00:00Z"
    assert client.get("/documents/external/post:42").json() == document


def test_put_chunks_and_embeds_the_content(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    document_id = uuid.UUID(
        _put(_client(app_with_database, tenant_with_key)).json()["document"]["id"]
    )

    chunks = asyncio.run(fetch_chunks(app_with_database.state.session_factory, document_id))

    assert [chunk.content for chunk in chunks] == [RECORD["content"]]


def test_put_without_optional_fields_stores_an_empty_object_and_no_timestamp(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    response = client.put("/documents/external/post:1", json={"title": "T", "content": "Text."})

    assert response.status_code == 201
    assert response.json()["document"]["metadata"] == {}
    assert response.json()["document"]["source_updated_at"] is None


def test_two_records_with_identical_text_are_two_documents(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    first = _put(client, "post:1")
    second = _put(client, "post:2")

    assert first.status_code == second.status_code == 201
    assert first.json()["document"]["id"] != second.json()["document"]["id"]
    assert len(client.get("/documents").json()) == 2


def test_the_same_id_in_two_tenants_creates_two_documents(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    first = _put(_client(app_with_database, tenant_with_key))
    second = _put(_client(app_with_database, second_tenant_with_key))

    assert first.status_code == second.status_code == 201
    assert first.json()["document"]["id"] != second.json()["document"]["id"]


@pytest.mark.parametrize(
    "changes",
    [
        {"title": ""},
        {"title": "x" * 501},
        {"content": ""},
        {"content": "   \n\t "},
        {"metadata": ["not", "an", "object"]},
        {"metadata": {"blob": "x" * 20_000}},
        {"source_updated_at": "2026-09-21T10:00:00"},  # no time zone
        {"source_updated_at": "yesterday"},
    ],
)
def test_put_rejects_an_invalid_body(
    app_with_database: FastAPI, tenant_with_key: str, changes: dict[str, Any]
) -> None:
    response = _put(_client(app_with_database, tenant_with_key), **changes)

    assert response.status_code == 422


def test_put_needs_a_title_and_content(app_with_database: FastAPI, tenant_with_key: str) -> None:
    client = _client(app_with_database, tenant_with_key)

    assert client.put("/documents/external/a", json={"content": "x"}).status_code == 422
    assert client.put("/documents/external/a", json={"title": "x"}).status_code == 422


def test_put_rejects_an_invalid_external_id(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    response = _put(_client(app_with_database, tenant_with_key), ".hidden")

    assert response.status_code == 422


def test_put_rejects_content_over_the_upload_limit(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(max_upload_size=10)

    response = _put(_client(app_with_database, tenant_with_key))

    assert response.status_code == 413


def test_the_upload_limit_counts_utf8_bytes_not_characters(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(max_upload_size=10)

    response = _put(_client(app_with_database, tenant_with_key), content="é" * 8)  # 16 bytes

    assert response.status_code == 413


def test_put_needs_authentication(app_with_database: FastAPI) -> None:
    response = TestClient(app_with_database).put("/documents/external/a", json=RECORD)

    assert response.status_code == 401


def test_a_synced_document_is_listed_and_deletable_by_uuid(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document_id = _put(client).json()["document"]["id"]

    assert [d["id"] for d in client.get("/documents").json()] == [document_id]
    assert client.delete(f"/documents/{document_id}").status_code == 204
    assert client.get("/documents/external/post:42").status_code == 404


class CountingEmbedder:
    """A ``FakeEmbedder`` that remembers every text it was asked to embed."""

    def __init__(self, dimension: int) -> None:
        self._inner = FakeEmbedder(dimension)
        self.texts: list[str] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return await self._inner.embed(texts)


@pytest.fixture
def embedder(app_with_database: FastAPI) -> CountingEmbedder:
    counting = CountingEmbedder(get_settings().embedding_dimension)
    app_with_database.dependency_overrides[get_embedder] = lambda: counting
    return counting


@pytest.fixture
def cache(app_with_database: FastAPI) -> FakeCache:
    fake = FakeCache()
    app_with_database.dependency_overrides[get_cache] = lambda: fake
    return fake


async def _tenant_id(app: FastAPI, name: str = "test-tenant") -> uuid.UUID:
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as session:
        return (await session.execute(select(Tenant.id).where(Tenant.name == name))).scalar_one()


def _corpus_version(app: FastAPI, cache: FakeCache) -> str | None:
    return asyncio.run(cache.get(f"corpus_version:{asyncio.run(_tenant_id(app))}"))


def _chunk_ids(app: FastAPI, document_id: str) -> list[uuid.UUID]:
    chunks = asyncio.run(fetch_chunks(app.state.session_factory, uuid.UUID(document_id)))
    return [chunk.id for chunk in chunks]


def test_sending_the_same_record_again_changes_nothing_and_embeds_nothing(
    app_with_database: FastAPI, tenant_with_key: str, embedder: CountingEmbedder
) -> None:
    client = _client(app_with_database, tenant_with_key)
    first = _put(client).json()["document"]
    embedded_before = list(embedder.texts)

    response = _put(client)

    assert response.status_code == 200
    assert response.json()["result"] == "unchanged"
    assert response.json()["document"]["id"] == first["id"]
    assert response.json()["document"]["updated_at"] == first["updated_at"]
    assert embedder.texts == embedded_before
    assert len(embedded_before) == 1


def test_a_new_title_updates_the_document_without_embedding(
    app_with_database: FastAPI, tenant_with_key: str, embedder: CountingEmbedder, cache: FakeCache
) -> None:
    client = _client(app_with_database, tenant_with_key)
    first = _put(client).json()["document"]
    chunks_before = _chunk_ids(app_with_database, first["id"])
    embedded_before = list(embedder.texts)
    version_before = _corpus_version(app_with_database, cache)

    response = _put(client, title="Returns policy")

    assert response.status_code == 200
    assert response.json()["result"] == "updated"
    assert response.json()["document"]["filename"] == "Returns policy"
    assert embedder.texts == embedded_before
    assert _chunk_ids(app_with_database, first["id"]) == chunks_before
    assert _corpus_version(app_with_database, cache) != version_before


def test_new_metadata_updates_the_document_without_touching_cached_answers(
    app_with_database: FastAPI, tenant_with_key: str, embedder: CountingEmbedder, cache: FakeCache
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client)
    embedded_before = list(embedder.texts)
    version_before = _corpus_version(app_with_database, cache)

    response = _put(client, metadata={"lang": "de"})

    assert response.json()["result"] == "updated"
    assert response.json()["document"]["metadata"] == {"lang": "de"}
    assert embedder.texts == embedded_before
    assert _corpus_version(app_with_database, cache) == version_before


def test_new_text_replaces_the_chunks_and_embeds_only_the_new_text(
    app_with_database: FastAPI, tenant_with_key: str, embedder: CountingEmbedder, cache: FakeCache
) -> None:
    client = _client(app_with_database, tenant_with_key)
    first = _put(client).json()["document"]
    old_chunks = _chunk_ids(app_with_database, first["id"])
    version_before = _corpus_version(app_with_database, cache)

    response = _put(client, content="Refunds now take 30 days.")

    assert response.status_code == 200
    assert response.json()["result"] == "replaced"
    assert response.json()["document"]["id"] == first["id"]
    assert response.json()["document"]["created_at"] == first["created_at"]
    assert embedder.texts[-1] == "Refunds now take 30 days."
    chunks = asyncio.run(
        fetch_chunks(app_with_database.state.session_factory, uuid.UUID(first["id"]))
    )
    assert [chunk.content for chunk in chunks] == ["Refunds now take 30 days."]
    assert not set(old_chunks) & {chunk.id for chunk in chunks}
    assert _corpus_version(app_with_database, cache) != version_before
    assert len(_documents(client)) == 1


def _documents(client: TestClient) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = client.get("/documents").json()
    return documents


def test_replacing_long_text_with_short_text_leaves_no_old_chunks(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    long_text = "\n\n".join(f"Paragraph {i}. " + "word " * 150 for i in range(12))
    document = _put(client, content=long_text).json()["document"]
    assert len(_chunk_ids(app_with_database, document["id"])) > 1

    _put(client, content="Short now.")

    chunks = asyncio.run(
        fetch_chunks(app_with_database.state.session_factory, uuid.UUID(document["id"]))
    )
    assert [(chunk.chunk_index, chunk.content) for chunk in chunks] == [(0, "Short now.")]


def test_a_newer_source_time_alone_is_remembered_without_embedding(
    app_with_database: FastAPI, tenant_with_key: str, embedder: CountingEmbedder
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client)
    embedded_before = list(embedder.texts)

    response = _put(client, source_updated_at="2026-09-22T08:00:00Z")

    assert response.json()["result"] == "unchanged"
    assert response.json()["document"]["source_updated_at"] == "2026-09-22T08:00:00Z"
    assert embedder.texts == embedded_before


def _force_status(app: FastAPI, document_id: str, status: str, *, drop_chunks: bool) -> None:
    async def run() -> None:
        session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
        async with session_factory() as session:
            document = await session.get(Document, uuid.UUID(document_id))
            assert document is not None
            document.status = status
            document.error = "boom" if status == "failed" else None
            if drop_chunks:
                await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
            await session.commit()

    asyncio.run(run())


def test_the_same_text_is_processed_again_after_a_failed_attempt(
    app_with_database: FastAPI, tenant_with_key: str, embedder: CountingEmbedder
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document = _put(client).json()["document"]
    _force_status(app_with_database, document["id"], "failed", drop_chunks=True)

    response = _put(client)

    assert response.json()["result"] == "replaced"
    assert response.json()["document"]["status"] == "ready"
    assert response.json()["document"]["error"] is None
    assert len(_chunk_ids(app_with_database, document["id"])) == 1
    assert len(embedder.texts) == 2


def test_the_same_text_is_not_queued_again_while_it_is_still_processing(
    app_with_database: FastAPI, tenant_with_key: str, embedder: CountingEmbedder
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document = _put(client).json()["document"]
    _force_status(app_with_database, document["id"], "processing", drop_chunks=False)

    response = _put(client)

    assert response.json()["result"] == "unchanged"
    assert response.json()["document"]["status"] == "processing"
    assert len(embedder.texts) == 1


def test_one_tenants_put_never_touches_another_tenants_document(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    first = _put(_client(app_with_database, tenant_with_key)).json()["document"]

    second = _put(_client(app_with_database, second_tenant_with_key), content="Other text.")

    assert second.json()["result"] == "created"
    assert (
        _client(app_with_database, tenant_with_key).get("/documents/external/post:42").json()
        == first
    )


def test_a_write_older_than_the_stored_one_is_ignored_and_reported(
    app_with_database: FastAPI, tenant_with_key: str, embedder: CountingEmbedder, cache: FakeCache
) -> None:
    client = _client(app_with_database, tenant_with_key)
    first = _put(client).json()["document"]
    embedded_before = list(embedder.texts)
    version_before = _corpus_version(app_with_database, cache)

    response = _put(
        client,
        title="Old title",
        content="An older version of the text.",
        metadata={"old": True},
        source_updated_at="2026-09-21T09:59:59Z",
    )

    assert response.status_code == 200
    assert response.json()["result"] == "stale"
    assert response.json()["document"] == first
    assert embedder.texts == embedded_before
    assert _corpus_version(app_with_database, cache) == version_before
    assert client.get("/documents/external/post:42").json() == first


def test_stale_is_judged_by_the_instant_not_by_the_text_of_the_timestamp(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client)  # stored: 10:00:00Z

    older = _put(client, source_updated_at="2026-09-21T11:00:00+02:00", title="A")  # 09:00Z
    same = _put(client, source_updated_at="2026-09-21T12:00:00+02:00", title="B")  # 10:00Z

    assert older.json()["result"] == "stale"
    assert same.json()["result"] == "updated"


def test_a_write_with_the_same_source_time_is_applied(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client)

    response = _put(client, content="Same instant, new text.")

    assert response.json()["result"] == "replaced"


def test_a_newer_write_is_applied_and_its_time_stored(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client)

    response = _put(client, content="Newer.", source_updated_at="2026-09-21T10:00:01Z")

    assert response.json()["result"] == "replaced"
    assert response.json()["document"]["source_updated_at"] == "2026-09-21T10:00:01Z"


def test_a_write_without_a_source_time_is_applied_and_keeps_the_stored_one(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client)

    response = client.put(
        "/documents/external/post:42", json={"title": "Retitled", "content": "Untimed text."}
    )

    assert response.json()["result"] == "replaced"
    assert response.json()["document"]["source_updated_at"] == "2026-09-21T10:00:00Z"
    stale = _put(
        client, content="Older than the kept time.", source_updated_at="2026-09-20T00:00:00Z"
    )
    assert stale.json()["result"] == "stale"


def test_a_stored_document_without_a_source_time_accepts_any_first_time(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    client.put("/documents/external/post:42", json={"title": "T", "content": "Untimed."})

    response = _put(client, source_updated_at="2001-01-01T00:00:00Z")

    assert response.json()["result"] == "replaced"
    assert response.json()["document"]["source_updated_at"] == "2001-01-01T00:00:00Z"


class RecordingQueue:
    """A job queue that only remembers jobs, so a test decides when the worker runs."""

    def __init__(self) -> None:
        self.jobs: list[tuple[uuid.UUID, str | None]] = []

    async def enqueue_process_document(
        self, document_id: uuid.UUID, sha256: str | None = None
    ) -> None:
        self.jobs.append((document_id, sha256))


class BrokenQueue:
    async def enqueue_process_document(
        self, document_id: uuid.UUID, sha256: str | None = None
    ) -> None:
        raise ConnectionError("redis is down")


class FailingEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding service is down")


LARGE_THRESHOLD = 20
"""Bytes above which content goes to the worker in these tests."""

OLD_TEXT = "Old text, small enough to embed at once."[:LARGE_THRESHOLD]
NEW_TEXT = "The new version of this record is long enough to be queued."
NEWER_TEXT = "An even newer version of this record, also long enough to queue."


@pytest.fixture
def queue(app_with_database: FastAPI) -> RecordingQueue:
    recording = RecordingQueue()
    app_with_database.dependency_overrides[get_job_queue] = lambda: recording
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        async_processing_threshold=LARGE_THRESHOLD
    )
    return recording


def _run_job(app: FastAPI, job: tuple[uuid.UUID, str | None], embedder: Any | None = None) -> None:
    settings = Settings(async_processing_threshold=LARGE_THRESHOLD)
    ctx: JobContext = {
        "session_factory": app.state.session_factory,
        "embedder": embedder or FakeEmbedder(settings.embedding_dimension),
        "cache": FakeCache(),
        "settings": settings,
    }
    asyncio.run(process_document(ctx, str(job[0]), job[1]))


def _stored(app: FastAPI, document_id: str) -> Document:
    async def run() -> Document:
        session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
        async with session_factory() as session:
            return (
                await session.execute(select(Document).where(Document.id == uuid.UUID(document_id)))
            ).scalar_one()

    return asyncio.run(run())


def _chunk_texts(app: FastAPI, document_id: str) -> list[str]:
    chunks = asyncio.run(fetch_chunks(app.state.session_factory, uuid.UUID(document_id)))
    return [chunk.content for chunk in chunks]


def test_large_content_is_queued_and_answered_with_202(
    app_with_database: FastAPI, tenant_with_key: str, queue: RecordingQueue
) -> None:
    client = _client(app_with_database, tenant_with_key)

    response = _put(client, content=NEW_TEXT)

    assert response.status_code == 202
    assert response.json()["result"] == "created"
    document = response.json()["document"]
    assert document["status"] == "pending"
    assert queue.jobs == [(uuid.UUID(document["id"]), content_hash(NEW_TEXT))]
    assert _chunk_texts(app_with_database, document["id"]) == []

    _run_job(app_with_database, queue.jobs[0])

    polled = client.get("/documents/external/post:42").json()
    assert polled["status"] == "ready"
    assert _chunk_texts(app_with_database, document["id"]) == [NEW_TEXT]
    assert _stored(app_with_database, document["id"]).raw_content is None


def test_content_at_the_threshold_is_still_processed_in_the_request(
    app_with_database: FastAPI, tenant_with_key: str, queue: RecordingQueue
) -> None:
    response = _put(_client(app_with_database, tenant_with_key), content="x" * LARGE_THRESHOLD)

    assert response.status_code == 201
    assert response.json()["document"]["status"] == "ready"
    assert queue.jobs == []


def test_the_old_version_stays_searchable_until_the_new_one_is_ready(
    app_with_database: FastAPI, tenant_with_key: str, queue: RecordingQueue
) -> None:
    client = _client(app_with_database, tenant_with_key)
    first = _put(client, content=OLD_TEXT).json()["document"]
    assert queue.jobs == []

    response = _put(client, content=NEW_TEXT)

    assert response.status_code == 202
    assert response.json()["result"] == "replaced"
    assert response.json()["document"]["status"] == "pending"
    assert _chunk_texts(app_with_database, first["id"]) == [OLD_TEXT]

    _run_job(app_with_database, queue.jobs[0])

    assert _chunk_texts(app_with_database, first["id"]) == [NEW_TEXT]
    assert client.get("/documents/external/post:42").json()["status"] == "ready"


def test_sending_the_same_large_text_again_does_not_queue_a_second_job(
    app_with_database: FastAPI, tenant_with_key: str, queue: RecordingQueue
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client, content=NEW_TEXT)

    response = _put(client, content=NEW_TEXT)

    assert response.status_code == 200
    assert response.json()["result"] == "unchanged"
    assert response.json()["document"]["status"] == "pending"
    assert len(queue.jobs) == 1


def test_a_job_for_an_older_version_does_nothing_and_only_the_newest_is_embedded(
    app_with_database: FastAPI, tenant_with_key: str, queue: RecordingQueue
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document = _put(client, content=NEW_TEXT).json()["document"]
    _put(client, content=NEWER_TEXT)
    embedder = CountingEmbedder(get_settings().embedding_dimension)
    assert len(queue.jobs) == 2

    _run_job(app_with_database, queue.jobs[0], embedder)  # for NEW_TEXT: superseded
    assert embedder.texts == []
    assert _stored(app_with_database, document["id"]).status == "pending"

    _run_job(app_with_database, queue.jobs[1], embedder)

    assert embedder.texts == [NEWER_TEXT]
    assert _chunk_texts(app_with_database, document["id"]) == [NEWER_TEXT]
    assert _stored(app_with_database, document["id"]).status == "ready"


def test_a_job_that_finds_a_newer_write_when_its_embedding_is_done_discards_its_work(
    app_with_database: FastAPI, tenant_with_key: str, queue: RecordingQueue
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document = _put(client, content=NEW_TEXT).json()["document"]

    class WriteArrivesMidway:
        """While the worker embeds, a newer PUT lands on the row."""

        async def embed(self, texts: list[str]) -> list[list[float]]:
            session_factory: async_sessionmaker[AsyncSession] = (
                app_with_database.state.session_factory
            )
            async with session_factory() as session:
                row = await session.get(Document, uuid.UUID(document["id"]))
                assert row is not None
                row.sha256 = content_hash(NEWER_TEXT)
                row.raw_content = NEWER_TEXT.encode()
                row.status = "pending"
                await session.commit()
            return await FakeEmbedder(get_settings().embedding_dimension).embed(texts)

    _run_job(app_with_database, queue.jobs[0], WriteArrivesMidway())

    row = _stored(app_with_database, document["id"])
    assert row.status == "pending"  # the newer write's job is still to come
    assert row.raw_content == NEWER_TEXT.encode()
    assert _chunk_texts(app_with_database, document["id"]) == []


def test_a_failed_job_keeps_the_old_version_and_the_same_text_is_retried(
    app_with_database: FastAPI, tenant_with_key: str, queue: RecordingQueue
) -> None:
    client = _client(app_with_database, tenant_with_key)
    first = _put(client, content=OLD_TEXT).json()["document"]
    _put(client, content=NEW_TEXT)

    _run_job(app_with_database, queue.jobs[0], FailingEmbedder())

    failed = client.get("/documents/external/post:42").json()
    assert failed["status"] == "failed"
    assert "embedding service is down" in failed["error"]
    assert _chunk_texts(app_with_database, first["id"]) == [OLD_TEXT]

    retry = _put(client, content=NEW_TEXT)

    assert retry.status_code == 202
    assert retry.json()["document"]["error"] is None
    _run_job(app_with_database, queue.jobs[1])
    assert _chunk_texts(app_with_database, first["id"]) == [NEW_TEXT]
    assert client.get("/documents/external/post:42").json()["status"] == "ready"


def test_a_failure_of_an_older_job_does_not_mark_a_newer_write_failed(
    app_with_database: FastAPI, tenant_with_key: str, queue: RecordingQueue
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document = _put(client, content=NEW_TEXT).json()["document"]

    class FailsAfterANewerWrite:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            session_factory: async_sessionmaker[AsyncSession] = (
                app_with_database.state.session_factory
            )
            async with session_factory() as session:
                row = await session.get(Document, uuid.UUID(document["id"]))
                assert row is not None
                row.sha256 = content_hash(NEWER_TEXT)
                row.status = "pending"
                await session.commit()
            raise RuntimeError("too late to matter")

    _run_job(app_with_database, queue.jobs[0], FailsAfterANewerWrite())

    row = _stored(app_with_database, document["id"])
    assert row.status == "pending"
    assert row.error is None


def test_when_the_queue_is_down_the_document_is_failed_so_a_retry_works(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        async_processing_threshold=LARGE_THRESHOLD
    )
    app_with_database.dependency_overrides[get_job_queue] = lambda: BrokenQueue()
    client = _client(app_with_database, tenant_with_key)

    down = _put(client, content=NEW_TEXT)

    assert down.status_code == 503
    stored = client.get("/documents/external/post:42").json()
    assert stored["status"] == "failed"
    assert "could not queue" in stored["error"]

    recording = RecordingQueue()
    app_with_database.dependency_overrides[get_job_queue] = lambda: recording

    again = _put(client, content=NEW_TEXT)

    assert again.status_code == 202
    assert len(recording.jobs) == 1
