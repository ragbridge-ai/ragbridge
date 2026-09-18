"""Tests for the ragbridge-admin command's underlying functions.

Each subcommand is a plain async function taking a session_factory, so
these call them directly rather than shelling out to the CLI - the same
level the rest of this codebase tests at (helpers.fetch_chunks, not the
upload endpoint's HTTP layer twice).
"""

import asyncio
import re
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge import admin
from ragbridge.auth import api_key_prefix


def _extract_key(output: str) -> str:
    match = re.search(r"API key: (\S+)", output)
    assert match is not None
    return match.group(1)


def test_create_tenant_prints_a_key_that_authenticates(
    app_with_database: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    asyncio.run(admin.create_tenant(session_factory, "acme"))
    key = _extract_key(capsys.readouterr().out)

    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {key}"})
    response = client.get("/documents")

    assert response.status_code == 200


def test_revoke_key_makes_it_stop_authenticating(
    app_with_database: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
    asyncio.run(admin.create_tenant(session_factory, "acme"))
    key = _extract_key(capsys.readouterr().out)

    asyncio.run(admin.revoke_key(session_factory, api_key_prefix(key)))

    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {key}"})
    response = client.get("/documents")

    assert response.status_code == 401


def test_revoke_key_fails_for_an_unknown_prefix(app_with_database: FastAPI) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    with pytest.raises(SystemExit):
        asyncio.run(admin.revoke_key(session_factory, "rb_doesnotexist"))


def test_create_key_adds_a_second_working_key_for_the_same_tenant(
    app_with_database: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
    asyncio.run(admin.create_tenant(session_factory, "acme"))
    match = re.search(r"Tenant (\S+)", capsys.readouterr().out)
    assert match is not None
    tenant_id = uuid.UUID(match.group(1))

    asyncio.run(admin.create_key(session_factory, tenant_id, "second-key"))
    second_key = _extract_key(capsys.readouterr().out)

    client = TestClient(app_with_database, headers={"Authorization": f"Bearer {second_key}"})
    response = client.get("/documents")

    assert response.status_code == 200


def test_create_key_fails_for_an_unknown_tenant(app_with_database: FastAPI) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory

    with pytest.raises(SystemExit):
        asyncio.run(admin.create_key(session_factory, uuid.uuid4(), "orphan"))


def test_list_tenants_prints_created_tenants(
    app_with_database: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    session_factory: async_sessionmaker[AsyncSession] = app_with_database.state.session_factory
    asyncio.run(admin.create_tenant(session_factory, "acme"))
    capsys.readouterr()

    asyncio.run(admin.list_tenants(session_factory))

    assert "acme" in capsys.readouterr().out
