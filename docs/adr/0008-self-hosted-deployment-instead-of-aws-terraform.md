# 8. Self-hosted deployment with Docker Compose and Caddy, instead of AWS and Terraform

Date: 2026-09-19

## Status

Accepted. Supersedes the roadmap's original "AWS infrastructure with Terraform".

## Context

The roadmap ended with AWS infrastructure defined in Terraform. There is no AWS
account, and AWS is not free. The available target is a single 2 vCPU / 4 GB virtual
server.

Terraform without an account can be *written* but never *applied* - not even
`terraform plan` runs without credentials - and `terraform validate` checks syntax
offline and proves nothing about whether the infrastructure works. This project's own
record argues against shipping that in a `v1.0.0`: ADRs 0004, 0005 and 0007 each exist
because something that looked right on paper failed when it was finally run.

Separately, the development setup was not safe on a public host. Reading it turned up:
Postgres and Redis published on every interface, a hard-coded database password and no
Redis password, an image running as root, no reverse proxy or TLS, a public `/docs`,
`ENVIRONMENT` declared but never read anywhere, and a CI that never built the image.

## Decision

1. **A single production Compose file (`docker-compose.prod.yml`) for any Linux host with
   Docker**, with Caddy terminating TLS. The development file is untouched.
2. **Safe defaults are enforced, not documented.** Compose refuses to start without
   `POSTGRES_PASSWORD` and `REDIS_PASSWORD`. The app is forced to
   `ENVIRONMENT=production`, and in that mode refuses to start with the database
   credentials published in `.env.example`. The rule is deliberately narrow - it rejects
   the *known shipped* credentials, not anything that merely looks weak, because a
   passwordless Redis on a private Docker network is a legitimate setup.
3. **`/docs` is a setting (`ENABLE_DOCS`), off in production**, rather than being disabled
   silently by `ENVIRONMENT`. Every endpoint already needs an API key, so the page
   exposes the API's shape, not its data; an operator can keep it.
4. **Migrations are a one-shot service** that must exit successfully before the app or
   worker start, so two containers can never race to migrate.
5. **CI runs a 21-check smoke test of the production stack** (`scripts/smoke-prod.sh`)
   that needs no language model, so a broken `Dockerfile` or Compose file can no longer
   reach `main` unnoticed.
6. **Terraform moves to "After v1"**: it can be added by anyone with an account, and the
   container image is identical either way.

## What building this found

Each of these was invisible in the plan and found only by running things.

- **Private files were baked into the image.** Local, untracked editor and
  agent configuration files (per-developer instructions) and `evaluation/` were copied into
  the runtime image; anyone who pulled it or read it on a server would have got them.
  `.dockerignore` now excludes them and any `.env.*`.
- **`.env.prod` was committable.** `.gitignore` covered only `.env`.
- **The worker healthcheck was a false reassurance.** With arq's default 3600-second
  heartbeat, a *crashed* worker (SIGKILL) kept reporting healthy for the whole
  observation; a clean stop did not, because arq removes its own heartbeat on shutdown,
  which had flattered the first test. A 30-second heartbeat detects a crash in about 35
  seconds.
- **A password-generation tip was wrong.** `openssl rand -base64` output goes inside a
  URL, and 10 of 20 sampled passwords contained a `/`, `+` or `=`; hex is URL-safe.
- **One smoke check gave false assurance.** The upload-limit test passed with the proxy
  limit removed, because the app enforces its own limit and also answers 413 - but only
  after reading the whole body into memory, which is what the proxy limit exists to
  prevent. The check now requires the proxy's own refusal. This was found by deliberately
  breaking each protection in turn and confirming the script failed on it (docs enabled,
  a published port, running as root, no Redis password, a defaulted secret, a private
  file in the image, the credential check disabled). A test that has only ever passed
  proves nothing.
- **A missing `OLLAMA_BASE_URL` failed opaquely.** Inside a container `localhost` is the
  container itself, so leaving it unset produced an HTTP 500 on the first embedding. The
  production file now defaults it to the host gateway, as the development file does.
- **`docker kill` does not restart a container** (Docker treats it as a manual stop); a
  genuine out-of-memory kill did restart the worker. Relevant when testing, not a defect.

## Consequences

- **Given up, stated honestly:** one-command cloud provisioning, and any Terraform in the
  repository.
- **Nothing has been run on a real server.** Verified: the stack on a laptop, and the
  smoke test on a GitHub Ubuntu runner, plus backup and restore, an upgrade with a code
  change, crash handling, and the upload limit. **Not verified:** a Let's Encrypt
  certificate (no domain), a hosted model provider (no key), Ollama reachable from
  containers on Linux, host firewall behaviour, latency and memory on server hardware,
  and an upgrade that changes the schema. `docs/deployment.md` states each of these next
  to the step it affects.
- **Sizing is measured, with limits.** The stack idles at about 573 MiB and peaked at
  about 800 MiB under load; `llama3.2` and `nomic-embed-text` reported 2.55 GB and 0.37 GB
  loaded, on macOS. A local chat model on a 4 GB server therefore leaves almost no
  headroom, so the recommended split is local embeddings and a hosted chat model. Those
  figures were not taken on server hardware.
- **Known limitations remain**, chiefly no rate limiting and a single app replica; both
  are listed in the deployment guide rather than papered over.
