"""Tests for setting a document's access groups: upload, PATCH, and PUT by external id."""

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.cache import FakeCache, get_cache
from ragbridge.config import Settings, get_settings
from ragbridge.db.models import Document
from ragbridge.jobs import get_job_queue
from tests.helpers import fetch_chunks

FILE = ("refunds.txt", b"Refunds take five days.", "text/plain")
TENANT_VERSION_PREFIX = "corpus_version:"


def _client(app: FastAPI, key: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def _chunk_groups(app: FastAPI, document_id: str) -> list[list[str]]:
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    chunks = asyncio.run(fetch_chunks(session_factory, uuid.UUID(document_id)))
    return [chunk.access_groups for chunk in chunks]


def _found(client: TestClient, groups: list[str] | None, query: str = "refunds") -> list[str]:
    body: dict[str, Any] = {"query": query}
    if groups is not None:
        body["access_groups"] = groups
    return [hit["filename"] for hit in client.post("/search", json=body).json()["results"]]


def _versions(app: FastAPI) -> dict[str, str]:
    cache = app.dependency_overrides[get_cache]()
    assert isinstance(cache, FakeCache)
    return {k: v for k, v in cache._values.items() if k.startswith(TENANT_VERSION_PREFIX)}  # noqa: SLF001


# --- upload


def test_an_upload_stores_its_groups_on_the_document_and_every_chunk(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    response = client.post(
        "/documents", files={"file": FILE}, data={"access_groups": ["hr", "public"]}
    )

    assert response.status_code == 201
    assert response.json()["access_groups"] == ["hr", "public"]
    assert _chunk_groups(app_with_database, response.json()["id"]) == [["hr", "public"]]
    assert _found(client, ["hr"]) == ["refunds.txt"]
    assert _found(client, ["other"]) == []


def test_an_upload_without_groups_has_none_and_is_found_only_without_a_scope(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    response = client.post("/documents", files={"file": FILE})

    assert response.json()["access_groups"] == []
    assert _chunk_groups(app_with_database, response.json()["id"]) == [[]]
    assert _found(client, None) == ["refunds.txt"]
    assert _found(client, ["public"]) == []


def test_upload_groups_are_sorted_and_deduplicated(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    response = _client(app_with_database, tenant_with_key).post(
        "/documents", files={"file": FILE}, data={"access_groups": ["public", "hr", "hr"]}
    )

    assert response.json()["access_groups"] == ["hr", "public"]


@pytest.mark.parametrize("groups", [["bad name!"], ["-x"], [f"g{n}" for n in range(33)]])
def test_an_upload_with_an_invalid_group_is_rejected_and_stores_nothing(
    app_with_database: FastAPI, tenant_with_key: str, groups: list[str]
) -> None:
    client = _client(app_with_database, tenant_with_key)

    response = client.post("/documents", files={"file": FILE}, data={"access_groups": groups})

    assert response.status_code == 422
    assert client.get("/documents").json() == []


def test_a_large_upload_keeps_its_groups_through_the_worker(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        async_processing_threshold=5
    )
    client = _client(app_with_database, tenant_with_key)

    response = client.post("/documents", files={"file": FILE}, data={"access_groups": ["hr"]})

    assert response.status_code == 202
    assert _chunk_groups(app_with_database, response.json()["id"]) == [["hr"]]


def test_uploading_the_same_file_again(app_with_database: FastAPI, tenant_with_key: str) -> None:
    client = _client(app_with_database, tenant_with_key)
    first = client.post(
        "/documents", files={"file": FILE}, data={"access_groups": ["hr", "public"]}
    )

    without_groups = client.post("/documents", files={"file": FILE})
    same_groups_other_order = client.post(
        "/documents", files={"file": FILE}, data={"access_groups": ["public", "hr"]}
    )
    other_groups = client.post("/documents", files={"file": FILE}, data={"access_groups": ["it"]})

    assert without_groups.status_code == same_groups_other_order.status_code == 200
    assert without_groups.json()["id"] == first.json()["id"]
    assert other_groups.status_code == 409
    assert "PATCH" in other_groups.json()["detail"]
    stored = client.get(f"/documents/{first.json()['id']}").json()
    assert stored["access_groups"] == ["hr", "public"]


def test_uploading_a_file_with_groups_that_was_stored_without_any_is_a_conflict(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    client.post("/documents", files={"file": FILE})

    response = client.post("/documents", files={"file": FILE}, data={"access_groups": ["hr"]})

    assert response.status_code == 409


# --- PATCH


def test_patch_changes_the_document_and_its_chunks_together(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document_id = client.post(
        "/documents", files={"file": FILE}, data={"access_groups": ["hr"]}
    ).json()["id"]

    response = client.patch(f"/documents/{document_id}", json={"access_groups": ["public", "it"]})

    assert response.status_code == 200
    assert response.json()["access_groups"] == ["it", "public"]
    assert _chunk_groups(app_with_database, document_id) == [["it", "public"]]
    assert _found(client, ["hr"]) == []
    assert _found(client, ["public"]) == ["refunds.txt"]


def test_patch_to_an_empty_list_hides_the_document_from_every_scope(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document_id = client.post(
        "/documents", files={"file": FILE}, data={"access_groups": ["hr"]}
    ).json()["id"]

    client.patch(f"/documents/{document_id}", json={"access_groups": []})

    assert _found(client, ["hr"]) == []
    assert _found(client, None) == ["refunds.txt"]


def test_patch_of_another_tenants_document_or_an_unknown_id_is_404(
    app_with_database: FastAPI, tenant_with_key: str, second_tenant_with_key: str
) -> None:
    document_id = (
        _client(app_with_database, tenant_with_key)
        .post("/documents", files={"file": FILE}, data={"access_groups": ["hr"]})
        .json()["id"]
    )
    other = _client(app_with_database, second_tenant_with_key)

    across = other.patch(f"/documents/{document_id}", json={"access_groups": ["public"]})
    unknown = other.patch(f"/documents/{uuid.uuid4()}", json={"access_groups": ["public"]})

    assert across.status_code == unknown.status_code == 404
    assert _chunk_groups(app_with_database, document_id) == [["hr"]]


@pytest.mark.parametrize("body", [{}, {"access_groups": "hr"}, {"access_groups": ["bad name!"]}])
def test_patch_requires_a_valid_list(
    app_with_database: FastAPI, tenant_with_key: str, body: dict[str, Any]
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document_id = client.post("/documents", files={"file": FILE}).json()["id"]

    assert client.patch(f"/documents/{document_id}", json=body).status_code == 422


def test_a_changed_group_set_invalidates_cached_answers_and_the_same_set_does_not(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    document_id = client.post(
        "/documents", files={"file": FILE}, data={"access_groups": ["hr", "public"]}
    ).json()["id"]
    before = _versions(app_with_database)

    client.patch(f"/documents/{document_id}", json={"access_groups": ["public", "hr"]})
    same_set_other_order = _versions(app_with_database)
    client.patch(f"/documents/{document_id}", json={"access_groups": ["hr"]})
    changed = _versions(app_with_database)

    assert same_set_other_order == before
    assert changed != before


def test_an_answer_cached_for_a_group_is_not_served_after_the_document_leaves_it(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        answer_cache_enabled=True
    )
    client = _client(app_with_database, tenant_with_key)
    document_id = client.post(
        "/documents", files={"file": FILE}, data={"access_groups": ["hr"]}
    ).json()["id"]
    ask = {"question": "refunds", "access_groups": ["hr"]}
    assert [s["filename"] for s in client.post("/query", json=ask).json()["sources"]] == [
        "refunds.txt"
    ]

    client.patch(f"/documents/{document_id}", json={"access_groups": ["public"]})

    assert client.post("/query", json=ask).json()["sources"] == []


class _ObservingCache(FakeCache):
    """Records the groups a document has, as another connection sees them, whenever the
    corpus version is bumped.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], document_id: uuid.UUID
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._document_id = document_id
        self.observing = False
        self.seen: list[list[str]] = []

    async def incr(self, key: str) -> int:
        if self.observing:
            async with self._session_factory() as session:
                document = await session.get(Document, self._document_id)
                assert document is not None
                self.seen.append(list(document.access_groups))
        return await super().incr(key)


def test_the_corpus_version_is_bumped_only_after_the_new_groups_are_committed(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    """Bumped first, a query between the bump and the commit would cache an answer built
    from the old groups under the new version, and it would stay there until it expired.
    """
    client = _client(app_with_database, tenant_with_key)
    document_id = client.post(
        "/documents", files={"file": FILE}, data={"access_groups": ["hr"]}
    ).json()["id"]
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
    cache = _ObservingCache(session_factory, uuid.UUID(document_id))
    app_with_database.dependency_overrides[get_cache] = lambda: cache
    cache.observing = True

    client.patch(f"/documents/{document_id}", json={"access_groups": ["public"]})
    seen_after_patch = list(cache.seen)

    client.put("/documents/external/post:1", json={"title": "post", "content": "Text."})
    external_id = client.get("/documents/external/post:1").json()["id"]
    cache.seen.clear()
    cache._document_id = uuid.UUID(external_id)  # noqa: SLF001
    client.put(
        "/documents/external/post:1",
        json={"title": "post", "content": "Text.", "access_groups": ["it"]},
    )

    assert seen_after_patch == [["public"]]
    assert cache.seen == [["it"]]
    assert _chunk_groups(app_with_database, document_id) == [["public"]]


# --- PUT by external id


def _put(client: TestClient, content: str = "Refunds take five days.", **fields: Any) -> Any:
    return client.put(
        "/documents/external/post:1", json={"title": "post", "content": content, **fields}
    )


def test_put_stores_groups_on_a_new_record_and_its_chunks(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    response = _put(client, access_groups=["public", "hr"])

    assert response.status_code == 201
    assert response.json()["document"]["access_groups"] == ["hr", "public"]
    assert _chunk_groups(app_with_database, response.json()["document"]["id"]) == [["hr", "public"]]


def test_put_without_the_field_keeps_the_groups_and_a_list_replaces_them(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client, access_groups=["hr"])

    kept = _put(client, "A new text for the record.", metadata={"a": 1})
    document_id = kept.json()["document"]["id"]
    replaced = _put(client, "A new text for the record.", access_groups=["public"])
    emptied = _put(client, "A new text for the record.", access_groups=[])

    assert kept.json()["document"]["access_groups"] == ["hr"]
    assert replaced.json()["document"]["access_groups"] == ["public"]
    assert emptied.json()["document"]["access_groups"] == []
    assert _chunk_groups(app_with_database, document_id) == [[]]


def test_a_new_record_without_the_field_has_no_groups(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    response = _put(_client(app_with_database, tenant_with_key))

    assert response.json()["document"]["access_groups"] == []


def test_a_groups_only_change_is_reported_as_updated_and_the_same_groups_as_unchanged(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client, access_groups=["hr"])

    changed = _put(client, access_groups=["public"])
    same = _put(client, access_groups=["public"])
    missing = _put(client)

    assert (changed.status_code, changed.json()["result"]) == (200, "updated")
    assert same.json()["result"] == "unchanged"
    assert missing.json()["result"] == "unchanged"


def test_new_text_with_new_groups_is_stored_under_the_new_groups(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client, access_groups=["hr"])

    response = _put(client, "A different text.", access_groups=["public"])

    assert response.json()["result"] == "replaced"
    assert _chunk_groups(app_with_database, response.json()["document"]["id"]) == [["public"]]


def test_a_stale_write_does_not_change_the_groups(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client, access_groups=["hr"], source_updated_at="2026-02-01T00:00:00Z")

    stale = _put(client, access_groups=["public"], source_updated_at="2026-01-01T00:00:00Z")

    assert stale.json()["result"] == "stale"
    assert stale.json()["document"]["access_groups"] == ["hr"]


def test_put_with_an_invalid_group_is_rejected(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)

    assert _put(client, access_groups=["bad name!"]).status_code == 422
    assert client.get("/documents").json() == []


class _NeverRunsQueue:
    """Accepts a job and never runs it: the document stays ``pending``."""

    async def enqueue_process_document(
        self, document_id: uuid.UUID, sha256: str | None = None
    ) -> None:
        return None


def test_a_change_of_groups_reaches_the_old_chunks_while_new_text_is_pending(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    client = _client(app_with_database, tenant_with_key)
    _put(client, access_groups=["hr"])
    app_with_database.dependency_overrides[get_settings] = lambda: Settings(
        async_processing_threshold=20
    )
    app_with_database.dependency_overrides[get_job_queue] = lambda: _NeverRunsQueue()

    response = _put(client, "A much longer replacement text for the record.", access_groups=["it"])

    assert response.status_code == 202
    assert response.json()["document"]["status"] == "pending"
    assert _chunk_groups(app_with_database, response.json()["document"]["id"]) == [["it"]]
    assert _found(client, ["hr"]) == []
    assert _found(client, ["it"]) == ["post"]
