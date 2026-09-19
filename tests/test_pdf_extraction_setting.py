"""Tests for the PDF_EXTRACTION setting and that uploads use it.

Kept in a module of its own rather than appended to test_config.py and
test_documents.py, so this change and the chunking change cannot conflict there.
"""

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ragbridge.config import Settings, get_settings
from tests.helpers import fetch_chunks
from tests.pdf_fixtures import build_two_column_cv_pdf

SHIPPED_URL = "postgresql+psycopg://ragbridge:ragbridge@localhost:5432/ragbridge"
COMPANY_A_ROW = "Mar 2022 - present Company A - Senior Engineer"


def test_pdf_extraction_defaults_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PDF_EXTRACTION", raising=False)

    assert Settings(database_url=SHIPPED_URL).pdf_extraction == "auto"


def test_an_unknown_pdf_extraction_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDF_EXTRACTION", "columns")

    with pytest.raises(ValidationError, match="pdf_extraction"):
        Settings(database_url=SHIPPED_URL)


def _stored_cv_text(app: FastAPI, key: str, settings: Settings) -> str:
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    upload = client.post(
        "/documents",
        files={"file": ("cv.pdf", build_two_column_cv_pdf("main_first"), "application/pdf")},
    )
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    chunks = asyncio.run(fetch_chunks(session_factory, uuid.UUID(upload.json()["id"])))
    return "\n".join(chunk.content for chunk in chunks)


def test_a_side_column_pdf_is_stored_with_dates_beside_their_company(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    stored = _stored_cv_text(app_with_database, tenant_with_key, Settings())

    assert COMPANY_A_ROW in stored


def test_pdf_extraction_plain_stores_the_text_in_file_order(
    app_with_database: FastAPI, tenant_with_key: str
) -> None:
    stored = _stored_cv_text(app_with_database, tenant_with_key, Settings(pdf_extraction="plain"))

    assert COMPANY_A_ROW not in stored
    assert "Mar 2022 - present" in stored
