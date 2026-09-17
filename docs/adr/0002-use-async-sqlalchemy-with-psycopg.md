# 2. Use async SQLAlchemy with psycopg

Date: 2026-09-17

## Status

Accepted

## Context

`ragbridge` spends most of its time waiting on I/O, not on CPU: every request to
`/documents` or `/query` waits on the database, and later phases add waits on an
embedding API and an LLM. A RAG service is a good fit for an async web server, because
one worker process can hold many requests in flight at once instead of one request per
process or per thread.

FastAPI supports both sync and async endpoints, and SQLAlchemy 2.x supports both a sync
and an async engine. The question is which to pick as the project's default from the
start.

- **Sync SQLAlchemy** (`psycopg2` or `psycopg` in sync mode): simpler to reason about,
  more existing examples online, but each request holds a worker (thread or process)
  for its whole duration, including idle time spent waiting on the database or an
  external API.
- **Async SQLAlchemy** with `psycopg` 3's async mode: a single worker process can serve
  many concurrent requests, because it switches to another request while one is waiting
  on I/O. This fits a service that is mostly waiting on network calls.

Switching from sync to async later is expensive: it changes the session type, every
query call site (`session.execute` becomes `await session.execute`), the test client,
Alembic's `env.py`, and any endpoint that touches the database. It is much cheaper to
decide this once, before there is much code to migrate.

## Decision

Use async SQLAlchemy 2.x with `psycopg` 3 (`psycopg[binary]`) as the only database
driver, everywhere: the FastAPI app, Alembic migrations, and tests. The connection URL
uses the `postgresql+psycopg://` dialect.

## Consequences

- One driver and one session type (`AsyncSession`) is used across the whole codebase -
  no mixed sync/async code paths to maintain.
- Every function that touches the database becomes an `async def`, and every call site
  needs `await`. Endpoints, dependencies, and helper functions in the database layer
  must all be async, all the way up the call stack.
- **PHP-FPM model vs. the Python async event loop.** A typical PHP-FPM deployment gives
  each request its own process (or thread), pulled from a fixed-size pool. While that
  process waits on a database query, it does nothing else - the OS scheduler moves on to
  other processes, but that specific worker is blocked and unavailable for other
  requests. Scaling concurrency means adding more PHP-FPM workers, each with its own
  memory footprint. Python's async event loop, in contrast, runs one worker process
  that holds many requests in flight: while one request awaits a database query, the
  event loop runs another request's code in the same process and same thread. This uses
  less memory per unit of concurrency, but it only helps when the code actually awaits
  I/O - CPU-bound work still blocks the whole event loop, unlike PHP-FPM workers which
  are naturally isolated from each other.
- **Async code is harder to debug.** A stack trace through async code crosses `await`
  points, so it looks less like a single linear call chain and more like a chain of
  suspended and resumed tasks. Forgetting a single `await` is a common, quiet bug: the
  code runs without raising an error but returns a coroutine object instead of a result.
  This is a real cost, accepted in exchange for the concurrency behavior above.
- Tests need a real running PostgreSQL, since the async driver and pgvector both matter
  to correctness; a lighter in-memory database (e.g. SQLite) is not accurate enough to
  stand in for it.
