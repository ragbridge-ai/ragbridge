"""Application settings, loaded from environment variables."""

from functools import lru_cache
from typing import Literal

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

    retrieval_mode: Literal["hybrid", "vector", "keyword"] = "hybrid"
    """Which retrieval arm(s) POST /query uses, unless the request overrides it.

    "hybrid" runs vector and keyword search and merges them with
    reciprocal rank fusion; "vector" or "keyword" runs only that one arm.
    """
    retrieval_candidates: int = 20
    """Rows each arm returns before fusion, in "hybrid" mode.

    Larger than top_k on purpose: fusion (and later, reranking) narrows a
    wide, cheap candidate set down to top_k - retrieving only top_k per
    arm would give a later reranker nothing extra to rerank.
    """

    rerank_enabled: bool = False
    """Whether POST /query reranks retrieved chunks before answering.

    Off by default: the default reranker (NoOpReranker) needs no provider
    or API key, so a fresh docker compose up with local Ollama keeps
    working unchanged - Ollama itself has no rerank endpoint anyway.
    """
    rerank_model: str = "cohere/rerank-v3.5"
    """LiteLLM rerank model name, used only when rerank_enabled is true."""

    redis_url: str = "redis://localhost:6379/0"
    """Connection used to enqueue and run background jobs (arq)."""
    async_processing_threshold: int = 100_000
    """Uploads larger than this many bytes are processed by the background
    worker instead of within the request (see ragbridge.jobs, ragbridge.worker).
    """

    embedding_cache_ttl: int = 86_400
    """Seconds a cached embedding lives. 0 disables the embedding cache.

    Safe to enable by default: an embedding is a pure function of
    (model, text), so a cached hit can never be stale (decision 7,
    docs/plans/phase-3.md).
    """
    answer_cache_enabled: bool = False
    """Whether POST /query caches whole answers, keyed by a per-tenant
    corpus version that any document upload or delete bumps.

    Off by default, unlike the embedding cache: a cached answer can go
    stale the moment a document is uploaded or deleted, so caching it
    is a deliberate tradeoff the operator opts into, not a free win.
    """
    answer_cache_ttl: int = 3_600
    """Seconds a cached answer lives, used only when answer_cache_enabled."""

    langfuse_public_key: str = ""
    """Enables Langfuse tracing and cost tracking when set together with
    langfuse_secret_key. Empty by default: tracing is absent, not merely
    disabled, until both keys are configured (decision 8,
    docs/plans/phase-3.md).
    """
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    """A self-hosted Langfuse instance can point this elsewhere."""

    agent_max_steps: int = 3
    """Hard ceiling on retrieval rounds per POST /agent call.

    Enforced by the loop itself, not by the prompt: a request may ask for
    fewer steps, never more (decision 4, docs/plans/phase-4.md).
    """
    agent_planner_model: str = ""
    """LiteLLM model name used to decide what to search for next.

    Empty means reuse chat_model. Planning needs reliable structured
    output and answering needs good prose, so an installation can point
    this at a stronger model without changing the answering model.
    """


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    ``lru_cache`` makes this a cheap singleton: the environment is only
    read once per process, and tests can bypass it by constructing
    ``Settings`` directly.
    """
    return Settings()
