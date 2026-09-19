# Development

## Running without Docker

```bash
uv sync
docker compose up -d postgres redis
uv run alembic upgrade head
uv run uvicorn ragbridge.main:app --reload

## Commands and tests

```bash
uv run pytest              # run tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy                # type check
scripts/smoke-prod.sh       # build and smoke-test the production stack (needs Docker)
```

Tests run against a real PostgreSQL with pgvector (`docker compose up -d postgres`),
in a **separate database**: `DATABASE_URL`'s database name plus `_test` (so `ragbridge`
becomes `ragbridge_test`), created and migrated automatically on the first run. The suite
empties every table before each test, so it refuses to run against any database whose name
does not end in `_test` - **your development data is never touched.** Tests never call a
real embedding provider, chat provider, queue, cache, or tracing
backend - see decision 5 in [docs/plans/phase-1.md](plans/phase-1.md).

## Evaluation

`evaluation/` has a small hand-written corpus and question set, plus two scripts:
retrieval quality (recall@k, MRR - needs only an embedder) and answer quality
(RAGAS faithfulness / answer relevancy / context precision / context recall - needs
a real judge LLM). Both run by hand against a live server, never in CI, and both
need `--api-key` (an API key from `ragbridge-admin`) since every endpoint requires
one - see [docs/evaluation.md](evaluation.md) for how to run them and the
latest results.

```bash
docker compose up -d
uv run ragbridge-admin create-tenant --name eval
uv run python -m evaluation.evaluate_retrieval --api-key <key>
uv run python -m evaluation.evaluate_answers --api-key <key>
```
