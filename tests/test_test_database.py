"""Tests for the machinery that keeps the suite away from the development database."""

import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from ragbridge.config import Settings, get_settings
from tests.database import assert_is_test_database, derive_test_database_url, safe_database_name

DEV_URL = "postgresql+psycopg://ragbridge:ragbridge@localhost:5432/ragbridge"


def test_the_test_database_is_the_dev_name_with_a_suffix() -> None:
    assert make_url(derive_test_database_url(DEV_URL)).database == "ragbridge_test"


def test_everything_but_the_database_name_is_kept() -> None:
    url = "postgresql+psycopg://alice:p%40ss@db.internal:6543/app"

    derived = make_url(derive_test_database_url(url))

    assert (derived.username, derived.password, derived.host, derived.port) == (
        "alice",
        "p@ss",
        "db.internal",
        6543,
    )
    assert derived.database == "app_test"


def test_a_name_that_is_already_a_test_database_is_left_alone() -> None:
    url = "postgresql+psycopg://u:p@h:5432/ragbridge_test"

    assert derive_test_database_url(url) == url


def test_deriving_twice_gives_the_same_url() -> None:
    once = derive_test_database_url(DEV_URL)

    assert derive_test_database_url(once) == once


def test_the_guard_refuses_a_development_database() -> None:
    with pytest.raises(RuntimeError, match="Refusing to empty tables"):
        assert_is_test_database(DEV_URL)


def test_the_guard_refuses_a_database_with_no_name() -> None:
    with pytest.raises(RuntimeError):
        assert_is_test_database("postgresql+psycopg://u:p@h:5432/")


def test_the_guard_accepts_a_test_database() -> None:
    assert_is_test_database("postgresql+psycopg://u:p@h:5432/ragbridge_test")


def test_a_database_name_with_odd_characters_is_not_used_in_create_database() -> None:
    with pytest.raises(RuntimeError, match="Unexpected characters"):
        safe_database_name('postgresql+psycopg://u:p@h:5432/x"; DROP DATABASE y; --')


def test_this_suite_is_really_running_against_a_test_database() -> None:
    """The point of it all: whatever ``.env`` or the environment says, the
    settings every other test reads name a ``_test`` database.
    """
    assert (make_url(Settings().database_url).database or "").endswith("_test")
    assert (make_url(get_settings().database_url).database or "").endswith("_test")


def test_the_suite_ignores_a_developers_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A local ``.env`` must not change what the tests see.

    The file is written to a directory the test works in, exactly where
    ``Settings`` would look for it if it were still reading ``.env``.
    """
    (tmp_path / ".env").write_text("RERANK_ENABLED=true\nRERANK_BACKEND=chat\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RERANK_ENABLED", raising=False)
    monkeypatch.delenv("RERANK_BACKEND", raising=False)

    settings = Settings()

    assert settings.rerank_enabled is False
    assert settings.rerank_backend == "api"


def test_importing_litellm_does_not_copy_the_env_file_into_the_environment() -> None:
    """``litellm`` loads ``.env`` into ``os.environ`` on import, but only in ``DEV`` mode."""
    assert os.environ["LITELLM_MODE"] != "DEV"
