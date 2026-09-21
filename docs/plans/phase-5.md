# Phase 5 — Deploy and release

This document plans Phase 5 from the [roadmap in AGENTS.md](../../AGENTS.md#4-roadmap).
Phases 1-4 built the service: RAG, quality, multi-tenancy and production plumbing, agents
and MCP. Phase 5 makes it something a stranger can put on a server, safely, from the
documentation alone, and ends with `v1.0.0`.

The roadmap said "AWS infrastructure with Terraform". There is no AWS account, and AWS is
not free. The real target is a **Hetzner CPX22**: 2 vCPU, 4 GB RAM, 80 GB disk, Ubuntu 24.
Decision 1 replaces the AWS item, and AGENTS.md is updated to match rather than left
contradicting this plan.

## Where things stand

Read out of the code, not assumed. All of this is fine for local development - and none of
it is safe on a public host:

| Gap | Where |
|---|---|
| `ENVIRONMENT` is declared but **never read anywhere** - a dead setting | `config.py` |
| `/docs` and `/openapi.json` are public and unauthenticated | `main.py` (FastAPI defaults) |
| Postgres `5432` and Redis `6379` published on every interface | `docker-compose.yml` |
| Database password hard-coded as `ragbridge`; Redis has no password | `docker-compose.yml` |
| Image runs as **root**, installs `uv` from the unpinned `:latest` tag | `Dockerfile` |
| Image carries tests and evaluation data; no `HEALTHCHECK` | `Dockerfile`, `.dockerignore` |
| `alembic upgrade head` runs inside the app's start command | `Dockerfile` `CMD` |
| No TLS, no reverse proxy | - |
| CI never builds the image, so a broken `Dockerfile` reaches `main` unnoticed | `ci.yml` |
| No rate limiting anywhere | - |

The dev Compose file is deliberately convenient - the Redis port is published so the app can
run outside Docker. That is why production needs **its own file**, not an edit of this one.

## Decisions

1. **A verified self-hosted deployment replaces AWS + Terraform.** One production Compose
   file, targeting the CPX22 and any other Linux host with Docker.
   *Why not write Terraform anyway:* without an account it can be written but never applied
   - not even `plan` runs without credentials. `terraform validate` checks syntax offline,
   and that is all it proves. This project's own record argues against shipping that in a
   `v1.0.0`: ADRs 0004, 0005 and 0007 each exist because something that looked right on
   paper failed when it was finally run.
   *Given up, stated honestly:* one-command cloud provisioning, and the "there is Terraform
   in this repo" line. Both can be added after v1 by anyone with an account; the container
   image is identical either way.
2. **Verification happens locally in this phase; the VPS deploy is a separate, later
   exercise.** The maintainer's choice. Every step below is therefore verified with Docker
   on a laptop, and **the guide labels which steps have been executed against a real VPS -
   at the end of this phase, none have.** This is a real limit on how much the guide can
   claim, and saying so is the point. What transfers from a laptop and what does not is
   spelled out in decision 5.
3. **Production-safe settings are code, not just documentation.** `ENVIRONMENT` stops being
   a dead string and becomes the switch that makes the app refuse to start with the
   credentials shipped in `.env.example`. Documentation that says "change the password" is
   advice; a process that will not boot with `ragbridge:ragbridge@` in its `DATABASE_URL` is
   a guarantee - the same reasoning as Phase 4 decision 4, where the agent's step ceiling
   went into code rather than into a prompt.
   The rule is deliberately **narrow**: reject the *known shipped defaults*, not "anything
   that looks weak". A passwordless Redis on a private Docker network is a legitimate
   setup, and a validator that guessed at weakness would block real deployments while
   giving no extra safety. Precision here is worth more than reach.
4. **A separate `docker-compose.prod.yml`; the dev file is untouched.** It differs from dev
   in exactly the ways the table above names: Postgres and Redis unpublished, secrets with
   **no defaults** (Compose refuses to start without them, so a forgotten secret is an
   error rather than `ragbridge`), Redis password required, a one-shot `migrate` service
   that must exit successfully before app and worker start, and **Caddy** terminating TLS.
5. **The model split is measured, and the measurement's limits are stated.** 4 GB is the
   binding constraint. Known sizes: `llama3.2` **2.02 GB**, `nomic-embed-text` **0.27 GB**;
   the rest of the stack is unmeasured, estimated at 1.1-1.8 GB. So embeddings clearly fit
   and a 3B chat model clearly does not fit comfortably - but that is arithmetic, not a
   measurement, and the difference matters.
   - **Measured locally, transfers to the VPS:** container RAM (`docker stats`) - identical
     images and identical model weights. Whether the stack fits in 4 GB, tested by capping
     the Docker VM's memory.
   - **Measured locally, does NOT transfer:** generation latency. A laptop CPU is not 2 vCPU
     of shared EPYC. Any local timing is labelled as such, and the guide ships a small
     script the maintainer can run on the VPS later to get the real number.
   The default the guide recommends (prior: local embeddings, hosted chat via LiteLLM) is
   only written down once the RAM measurement supports it.
6. **`/docs` becomes a setting, not a production ban.** A new `ENABLE_DOCS` (default `true`,
   set to `false` by the production Compose file). Every endpoint already requires an API
   key, so a public `/docs` page leaks the API's *shape*, not its data - real, but small.
   Silently changing behaviour based on `ENVIRONMENT` would be the more surprising choice;
   an explicit switch lets an operator keep the docs page if they want it.
7. **The demo and the agent measurement are real runs, not illustrations.** `docs/demo.md`
   is copy-pasteable, and its printed output is captured from an actual run against Ollama.
   And `/agent` is finally measured against `/query` with the Phase 2 evaluation set -
   **the result is published whatever it says.** The README currently admits agent answer
   quality "has not been evaluated", which is true and awkward in a 1.0. ADR 0006 measured
   only that the planner's JSON parses, never that multi-step retrieval produces better
   answers. If it does not help on the default model, the docs say so plainly.
8. **`v1.0.0` means "safe to follow the docs", not "feature complete".** Release checklist:
   CI green including the new image/deployment job, the production stack brought up from a
   clean clone, every documented step actually executed, measurements published, and known
   limitations written down.

## Not in scope

- **Terraform / AWS** - decision 1. Moves to "After v1".
- **A web UI.** The project is an API service (AGENTS.md section 1); FastAPI's `/docs` is
  the interactive surface. A UI is a feature of its own, not a release task.
- **Rate limiting, autoscaling, multiple replicas.** Documented as limitations, not built.
  A public deployment should rate-limit at the proxy; that belongs in the guide.
- **Real certificate issuance.** No domain exists yet. Caddy is configured so that switching
  from a local test certificate to a real one is a single environment variable, and the
  guide marks the real-domain path **unverified** until someone runs it.
- **Near-duplicate planner queries.** An exact-repeat check would not catch the near-repeat
  seen in Phase 4 (`support hours` after `What are the support hours?`); catching it needs
  semantic similarity, which is a larger change than a release phase should carry.
- **Publishing.** Pushing images or a PyPI package is a maintainer action. Step 4 confirms
  `uv build` produces a valid package; whether to publish is the maintainer's call.

## Data model changes

None.

## API changes

None. `/docs` and `/openapi.json` become switchable (decision 6), which changes whether two
non-API pages are served, not any endpoint's behaviour.

## New settings

| Setting | Default | Meaning |
|---|---|---|
| `ENVIRONMENT` | `development` | Now typed (`development` \| `production`). In `production`, the app refuses to start with the credentials shipped in `.env.example`. |
| `ENABLE_DOCS` | `true` | Serve `/docs` and `/openapi.json`. The production Compose file sets it to `false`. |

The production Compose file additionally reads `POSTGRES_PASSWORD`, `REDIS_PASSWORD` and the
site address from its own environment. Those configure the *deployment*, not
`ragbridge.config.Settings`.

## Steps

1. Production-safe settings (code only, no Docker)
2. Image hardening and `docker-compose.prod.yml`
3. CI: build the image and smoke-test the production stack
4. Measurements - resource footprint, `/agent` vs `/query` - and the captured demo
5. Deployment guide, ADR 0008, README/AGENTS.md, and the `v1.0.0` wrap-up

Each step is built as several small, focused commits on its own feature branch.

## Step 1 detail — production-safe settings

Branch: `feat/phase-5-production-settings`. Code only: no Dockerfile or Compose changes, so
it lands and is testable on its own. Nothing here changes default behaviour - a developer
who sets no new variables sees exactly today's application.

Proposed commit breakdown:

1. **`feat(config): type ENVIRONMENT and reject shipped defaults in production`**
   - `environment: Literal["development", "production"]`, so a typo like `prod` fails at
     startup instead of silently disabling every check below.
   - A Pydantic model validator that, **only** when `environment == "production"`, rejects a
     `database_url` still carrying the `ragbridge:ragbridge@` credentials from
     `.env.example`. The error names the variable and what to do about it.
   - Deliberately *not* rejected: a passwordless Redis (decision 3), a missing Langfuse key,
     or anything else a legitimate private-network deployment may want.
   - Tests: production + default credentials raises, and the message names `DATABASE_URL`;
     production + a real password is accepted; **development + default credentials is
     accepted** (this is the test that proves local work is unaffected); an unknown
     `ENVIRONMENT` value raises.
2. **`feat(config): add ENABLE_DOCS to control the public API docs`**
   - `enable_docs: bool = True`; `create_app()` passes `docs_url=None, openapi_url=None`
     when it is false.
   - Tests: default serves `/docs` and `/openapi.json` (200); disabled returns 404 for both;
     **and `/health` still works when docs are off**, proving the app itself is unaffected.
3. **`docs: document the production settings in .env.example`**
   - Both variables with a comment explaining what production mode refuses, and a
     `openssl rand` one-liner for generating the passwords the next step will require.

Steps 2-5 get their own detailed breakdown when reached, as Phase 4's steps did.

## Outcome

Written after the phase, so the plan above stays as it was decided and this records where
building it disagreed.

**Where reality differed from the plan**

- The gap table said the image carried tests and evaluation data. Tests were already in
  `.dockerignore`; the real leak was local, untracked editor and agent
  configuration files and `evaluation/`.
- The plan put the `HEALTHCHECK` in the Dockerfile. It went in the Compose file instead:
  one image serves both the app and the worker, so an image-level check would mark the
  worker unhealthy. The worker got its own check (`arq --check`).
- The plan did not foresee that arq's default heartbeat is 3600 seconds, which made that
  check report a crashed worker healthy for up to an hour. `WorkerSettings` now uses 30.
- Step 1's advice to generate passwords with `openssl rand -base64` was wrong for
  passwords that sit inside URLs; corrected to `-hex`.
- Decision 7 planned to measure `/agent` on the Phase 2 evaluation set. That set is six
  documents of about one chunk each, so a single `/query` already returns 5 of the 6 and
  the comparison would have been a meaningless tie. A new multi-hop evaluation
  (`evaluation/evaluate_agent.py`) was built instead.
- The CI smoke test's first upload-limit check passed with the proxy limit removed. Each
  check was then mutation-tested (each protection deliberately broken) and that one was
  tightened.
- Decision 5 said RAM measurements transfer to the server. They do in principle, but they
  were taken on macOS, where Ollama reports GPU memory; the guide says so.

**Results**

- `/agent` with the default `llama3.2` planner was **not meaningfully better** than
  `/query` on multi-step questions (see `docs/evaluation.md`); the loop works and the
  planner is the weak part. Published as measured.
- The stack idles at about 573 MiB and peaked near 800 MiB; a local chat model on a 4 GB
  server leaves almost no headroom.
- The smoke test runs 21 checks in CI on a GitHub Ubuntu runner as well as locally.

**Still not done - stated, not hidden**

Nothing has been run on a real server. Not verified: a Let's Encrypt certificate (no
domain), a hosted model provider (no key), Ollama reachable from containers on Linux, host
firewall behaviour, latency and memory on server hardware, and an upgrade that changes the
schema. `docs/deployment.md` lists each next to the step it affects. `v1.0.0` here means
"safe to follow the docs" (decision 8), not "proven on a server".
