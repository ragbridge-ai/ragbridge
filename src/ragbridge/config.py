"""Application settings, loaded from environment variables."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

SHIPPED_DB_CREDENTIALS = ("ragbridge", "ragbridge")
"""The username and password in ``.env.example``'s ``DATABASE_URL``.

Convenient locally, and a public host reachable by anyone who has read
this project's README. ``ENVIRONMENT=production`` refuses them.
"""


class Settings(BaseSettings):
    """Typed application settings.

    Values are read from environment variables (or a local ``.env`` file).
    See ``.env.example`` for the full list of supported variables.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ragbridge"
    environment: Literal["development", "production"] = "development"
    """Which safety checks apply. ``production`` refuses to start with the
    credentials shipped in ``.env.example`` (see the validator below).

    Typed rather than a free string so a misspelling like ``prod`` fails
    at startup instead of silently turning every production check off.
    """
    database_url: str = "postgresql+psycopg://ragbridge:ragbridge@localhost:5432/ragbridge"
    max_upload_size: int = 10_000_000
    """Maximum accepted size, in bytes, of an uploaded document."""

    pdf_extraction: Literal["auto", "plain", "layout"] = "auto"
    """How PDF pages are read (see ``ragbridge.pdf.extract_pdf_pages``).

    ``auto`` reads each page row by row, so a side column of dates and cities
    stays beside its text, and keeps pypdf's plain order for pages of two
    columns of prose. ``plain`` is pypdf's default for every page; ``layout``
    always reads row by row. Only documents uploaded after a change are
    affected.
    """

    enable_docs: bool = True
    """Serve the generated API docs at /docs and /openapi.json.

    A deliberate switch rather than something ``environment`` turns off
    silently (decision 6, docs/plans/phase-5.md). Every endpoint already
    requires an API key, so a public docs page exposes the API's shape,
    not its data - the production Compose file turns it off, and an
    operator who wants it can keep it.
    """

    enable_playground: bool = True
    """Serve the playground page at /playground (docs/plans/phase-6-ui.md).

    A page for trying the service on your own files and seeing what
    retrieval did - a development and demonstration tool, not a product
    surface. An explicit switch, like ``enable_docs``, rather than
    something ``environment`` turns off silently: the production Compose
    file sets it to false, and an operator who wants it can keep it.
    """

    embedding_model: str = "ollama/nomic-embed-text"
    """LiteLLM model name used to embed chunks."""
    embedding_dimension: int = 768
    """Size of the embedding vector. Must match embedding_model's output.

    Fixed per installation: changing it requires a new migration for the
    chunks.embedding column and re-embedding every existing chunk.
    """
    chat_model: str = "ollama/llama3.2"
    """LiteLLM model name used to answer questions (from step 5 on)."""
    chat_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    """Sampling temperature of the answer model (``POST /query``, and the answer of ``/agent``).

    0 makes the model pick its most likely word every time, so asking the same question
    over the same context gives the same answer. Without it a provider uses its own
    default (Ollama: 0.8), and a small local model then reads the same bullet under a
    different company's heading from one run to the next. Raise it only if you want
    varied wording. Providers that reject the parameter simply do not receive it.
    """
    ollama_base_url: str = "http://localhost:11434"

    chunk_size: int = 1000
    """Maximum characters per chunk, before overlap. See ragbridge.chunking."""
    chunk_overlap: int = 200
    """Characters repeated between consecutive chunks of the same paragraph."""
    chunk_min_size: int = 100
    """A chunk shorter than this is merged into its neighbour at ingestion.

    A name, a city or a lone URL says almost nothing on its own yet competes
    with real content in search. 0 turns merging off. A merged chunk can
    exceed ``chunk_size`` by less than this. Only documents uploaded after a
    change are affected: existing chunks are not rebuilt.
    """

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
    keyword_max_term_frequency: float = Field(default=0.5, gt=0.0, le=1.0)
    """A word of a keyword query that is in more than this share of the tenant's chunks is
    left out of it. 1.0 turns that off.

    A product name, or a word like "project", is in nearly every generic chunk of a
    document set: it says nothing about which chunk answers, and it lets every generic
    chunk match the keyword search and take keyword credit. Left out, only the words that
    tell chunks apart decide. Not applied below 20 chunks (frequency means nothing yet),
    to a query that is only common words, or to a quoted phrase or a ``-word``.
    """

    keyword_rarity_weighting: bool = True
    """Rank the keyword search's OR fallback by how rare the matched words are.

    PostgreSQL's ``ts_rank`` weighs every word the same, so a chunk that matches three
    common words (api, documentation, page) outranks the one chunk with the single rare
    word the question is about. With this on, each matched word counts
    ``ln(chunks / chunks containing it)``. Only the OR fallback is affected, and only from
    20 chunks up.
    """

    rerank_enabled: bool = False
    """Whether POST /query reranks retrieved chunks before answering.

    Off by default: the default reranker (NoOpReranker) needs no provider
    or API key, so a fresh docker compose up with local Ollama keeps
    working unchanged - Ollama itself has no rerank endpoint anyway.
    """
    rerank_model: str = "cohere/rerank-v3.5"
    """LiteLLM rerank model name, used only when rerank_enabled is true."""

    answer_context_neighbours: int = 1
    """How many chunks before and after each retrieved chunk POST /query also gives
    the answer model, stitched into one continuous excerpt. 0 turns it off.

    A chunk often ends with a heading whose bullets start the next chunk; without
    its neighbours the model can attach the bullets to the wrong heading.
    """
    answer_context_max_chars: int = 6_000
    """Ceiling, in characters, on the neighbours added to the answer context.

    Neighbours are only added while the context stays within it (the retrieved
    chunks are always kept): a local model has a small context window, and one that
    overflows loses the start of the prompt.
    """

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

    @model_validator(mode="after")
    def _check_chunk_settings(self) -> Self:
        """Fail at startup, not with an HTTP 500 on the first upload.

        ``chunk_text`` enforces the same two rules; checking them here as
        well means a wrong value stops the app from starting.
        """
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        if self.chunk_min_size >= self.chunk_size:
            raise ValueError("CHUNK_MIN_SIZE must be smaller than CHUNK_SIZE")
        return self

    @model_validator(mode="after")
    def _reject_shipped_defaults_in_production(self) -> Self:
        """Refuse to start in production with the credentials from ``.env.example``.

        Documentation that says "change the password" is advice; a
        process that will not boot with the published one is a
        guarantee - the same reasoning that put the agent's step ceiling
        in code rather than in a prompt (decision 3,
        docs/plans/phase-5.md).

        Deliberately narrow: it rejects the *known shipped* credentials,
        not everything that looks weak. A passwordless Redis on a private
        Docker network is a legitimate deployment, and guessing at
        weakness would block real setups without adding safety.
        """
        if self.environment != "production":
            return self

        url = make_url(self.database_url)
        if (url.username, url.password) == SHIPPED_DB_CREDENTIALS:
            raise ValueError(
                "DATABASE_URL still uses the username and password from .env.example, "
                "which are public. Set a generated password (for example "
                "`openssl rand -hex 24`, which is safe inside a URL) before running "
                "with ENVIRONMENT=production."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance.

    ``lru_cache`` makes this a cheap singleton: the environment is only
    read once per process, and tests can bypass it by constructing
    ``Settings`` directly.
    """
    return Settings()
