"""Tests for production-safe settings validation.

Pure ``Settings`` construction: no database, no network, no app.
"""

import pytest
from pydantic import ValidationError

from ragbridge.config import Settings

SHIPPED_URL = "postgresql+psycopg://ragbridge:ragbridge@localhost:5432/ragbridge"
SAFE_URL = "postgresql+psycopg://ragbridge:s3cret-generated@db:5432/ragbridge"


def test_development_accepts_the_shipped_credentials() -> None:
    """The point of the narrow rule: local work is completely unaffected."""
    settings = Settings(environment="development", database_url=SHIPPED_URL)

    assert settings.database_url == SHIPPED_URL


def test_development_is_the_default_environment() -> None:
    assert Settings(database_url=SHIPPED_URL).environment == "development"


def test_production_rejects_the_shipped_credentials() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(environment="production", database_url=SHIPPED_URL)

    message = str(error.value)
    assert "DATABASE_URL" in message
    assert ".env.example" in message


def test_production_accepts_a_real_password() -> None:
    settings = Settings(environment="production", database_url=SAFE_URL)

    assert settings.environment == "production"


def test_production_accepts_the_shipped_username_with_a_different_password() -> None:
    """Only the pair is rejected - keeping the ``ragbridge`` role name is fine."""
    url = "postgresql+psycopg://ragbridge:a-real-password@db:5432/ragbridge"

    assert Settings(environment="production", database_url=url).database_url == url


def test_production_does_not_care_about_a_passwordless_redis() -> None:
    """Deliberately not rejected: legitimate on a private Docker network."""
    settings = Settings(
        environment="production", database_url=SAFE_URL, redis_url="redis://redis:6379/0"
    )

    assert settings.redis_url == "redis://redis:6379/0"


def test_an_unknown_environment_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must fail loudly, not silently disable the checks above.

    Set through the environment, which is how a real typo arrives -
    mypy already rejects a misspelling written in Python.
    """
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("DATABASE_URL", SAFE_URL)

    with pytest.raises(ValidationError):
        Settings()


def test_production_rejects_the_shipped_credentials_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real path: both values arrive as environment variables."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", SHIPPED_URL)

    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings()


def test_the_playground_is_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_PLAYGROUND", raising=False)

    assert Settings(database_url=SHIPPED_URL).enable_playground is True


def test_the_playground_can_be_disabled_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_PLAYGROUND", "false")

    assert Settings(database_url=SHIPPED_URL).enable_playground is False
