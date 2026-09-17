"""Application settings, loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings.

    Values are read from environment variables (or a local ``.env`` file).
    See ``.env.example`` for the full list of supported variables.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ragbridge"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://ragbridge:ragbridge@localhost:5432/ragbridge"


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    ``lru_cache`` makes this a cheap singleton: the environment is only
    read once per process, and tests can bypass it by constructing
    ``Settings`` directly.
    """
    return Settings()
