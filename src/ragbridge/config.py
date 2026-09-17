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
    max_upload_size: int = 10_000_000
    """Maximum accepted size, in bytes, of an uploaded document."""

    embedding_model: str = "ollama/nomic-embed-text"
    """LiteLLM model name used to embed chunks."""
    embedding_dimension: int = 768
    """Size of the embedding vector. Must match embedding_model's output.

    Fixed per installation: changing it requires a new migration for the
    chunks.embedding column and re-embedding every existing chunk.
    """
    chat_model: str = "ollama/llama3.2"
    """LiteLLM model name used to answer questions (from step 5 on)."""
    ollama_base_url: str = "http://localhost:11434"

    chunk_size: int = 1000
    """Maximum characters per chunk, before overlap. See ragbridge.chunking."""
    chunk_overlap: int = 200
    """Characters repeated between consecutive chunks of the same paragraph."""


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    ``lru_cache`` makes this a cheap singleton: the environment is only
    read once per process, and tests can bypass it by constructing
    ``Settings`` directly.
    """
    return Settings()
