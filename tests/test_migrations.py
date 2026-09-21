"""Checks that migrations were applied to the database.

The session fixture in conftest.py creates the ``<name>_test`` database and runs
`alembic upgrade head` against it before any test runs, so this checks that the
migrations actually produced what the app needs.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from sqlalchemy.engine import make_url

from ragbridge.config import Settings
from ragbridge.db.session import create_engine
from tests.database import derive_test_database_url, safe_database_name

PROJECT_ROOT = Path(__file__).parent.parent


def test_vector_extension_is_installed() -> None:
    async def vector_extension_exists() -> bool:
        engine = create_engine(Settings())
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            )
            exists = result.first() is not None
        await engine.dispose()
        return exists

    assert asyncio.run(vector_extension_exists())


def _run_alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def scratch_database_url() -> Iterator[str]:
    """An empty, throwaway database, so a migration can be run up and down safely.

    Its name ends in ``_test``, like the suite's own database, and it is dropped
    afterwards. The suite's database is never downgraded.
    """
    url = derive_test_database_url(Settings().database_url)
    scratch = make_url(url).set(database=f"{make_url(url).database}_migration")
    admin = create_sync_engine(scratch.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = safe_database_name(scratch.render_as_string(hide_password=False))
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    yield scratch.render_as_string(hide_password=False)
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _scalar(url: str, sql: str) -> object:
    engine = create_sync_engine(url)
    with engine.connect() as connection:
        value = connection.execute(text(sql)).scalar()
    engine.dispose()
    return value


def test_the_external_id_migration_keeps_existing_documents(scratch_database_url: str) -> None:
    url = scratch_database_url
    assert _run_alembic(url, "upgrade", "c39ac7386406").returncode == 0
    engine = create_sync_engine(url)
    with engine.begin() as connection:
        tenant_id = uuid.uuid4()
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'acme')"), {"id": tenant_id}
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, filename, content_type, sha256, status) "
                "VALUES (:id, :tenant, 'old.txt', 'text/plain', 'abc', 'ready')"
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id},
        )
    engine.dispose()

    result = _run_alembic(url, "upgrade", "head")

    assert result.returncode == 0, result.stderr
    assert _scalar(url, "SELECT external_id FROM documents") is None
    assert _scalar(url, "SELECT metadata::text FROM documents") == "{}"


def test_the_external_id_migration_downgrades_only_without_synced_documents(
    scratch_database_url: str,
) -> None:
    url = scratch_database_url
    assert _run_alembic(url, "upgrade", "head").returncode == 0
    engine = create_sync_engine(url)
    with engine.begin() as connection:
        tenant_id = uuid.uuid4()
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'acme')"), {"id": tenant_id}
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, filename, content_type, sha256, "
                "external_id, status) VALUES (:id, :tenant, 't', 'text/plain', 'abc', 'post:1', "
                "'ready')"
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id},
        )

    refused = _run_alembic(url, "downgrade", "-1")

    assert refused.returncode != 0
    assert "external_id" in refused.stderr
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM documents"))
    engine.dispose()
    assert _run_alembic(url, "downgrade", "-1").returncode == 0
    assert _run_alembic(url, "upgrade", "head").returncode == 0
