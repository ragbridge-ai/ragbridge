# Deployment

How to run ragbridge on a single Linux server with Docker. This guide describes only
what was actually run; **the last section lists, step by step, what was verified and
what was not.** Read it before trusting a step, because the most important limit is
this one: **nothing in this guide has been run on a real VPS yet.** It was run with
Docker on a laptop and, for the smoke test, on a GitHub Actions Ubuntu runner.

## What you get

`docker-compose.prod.yml` starts five long-running services and one one-shot job:

| Service | Role | Published to the internet? |
|---|---|---|
| `caddy` | TLS reverse proxy; rejects oversized bodies; security headers | **Yes** - ports 80 and 443 |
| `app` | The API and the `/mcp` endpoint | No |
| `worker` | Processes large uploads in the background | No |
| `postgres` | PostgreSQL with pgvector | No |
| `redis` | Job queue and cache, password-protected | No |
| `migrate` | Runs `alembic upgrade head` once, then exits; app and worker wait for it | No |

It differs from the development `docker-compose.yml` on purpose: the app is forced into
`ENVIRONMENT=production` (it refuses to start with the database credentials published
in `.env.example`), the API docs page is off, Compose refuses to start without the two
secrets, the image runs as an unprivileged user, and container logs are size-capped.
[docs/plans/phase-5.md](plans/phase-5.md) explains why.

## Sizing

Measured with `docker stats` on the stack under an evaluation workload (60 documents
embedded, then a couple of hundred questions through `/query` and `/agent`):

| Component | Idle | Peak observed |
|---|---|---|
| app | 284 MiB | 290 MiB |
| worker | 240 MiB | 441 MiB |
| postgres | 26 MiB | 35 MiB |
| redis | 13 MiB | 15 MiB |
| caddy | 10 MiB | 18 MiB |
| **Stack total** | **≈ 573 MiB** | **≈ 800 MiB** (sum of per-container peaks, so an upper bound) |

A model server is separate. `ollama ps` reported **2.55 GB** for `llama3.2` and
**0.37 GB** for `nomic-embed-text` when loaded. For a **4 GB** server:

| Setup | Approximate memory | Verdict |
|---|---|---|
| Stack + local embeddings, hosted chat model | ≈ 1.2 GB | Comfortable |
| Stack + local embeddings + local `llama3.2` | ≈ 3.7 GB | **No headroom** for the OS, Docker daemon and page cache. Not recommended. |

`qwen2.5:7b`, the best model measured on a 16 GB laptop (see [evaluation.md](evaluation.md)),
loads at **4.74 GB**, so it does not fit alongside the stack on a 4 GB server either.

So on a 2 vCPU / 4 GB server the recommended split is **local embeddings and a hosted
chat model** (set `CHAT_MODEL`, and the provider's API key, in `.env.prod`).

**Caveats, so these numbers are not over-trusted.** They were measured on macOS, where
Ollama reports memory it holds on the GPU; a CPU-only Linux server may differ. Operating
system overhead was not measured. **Response latency was not measured on server
hardware at all** - a laptop CPU says nothing about 2 shared vCPUs. Measure it yourself
after deploying:

```bash
curl -s -o /dev/null -w 'query: %{time_total}s\n' -X POST "$URL/query" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"question": "a question your documents answer"}'
```

## First deployment

You need a Linux host with Docker Engine and the Compose plugin (follow Docker's own
installation instructions for your distribution), and this repository on it.

```bash
cp .env.prod.example .env.prod
```

Edit `.env.prod`:

1. **Secrets.** Generate each with `openssl rand -hex 24`. Use hex, not base64: the
   passwords sit inside URLs, and base64's `/ + =` are URL delimiters that some parsers
   misread.
   ```bash
   POSTGRES_PASSWORD=<output of openssl rand -hex 24>
   REDIS_PASSWORD=<output of openssl rand -hex 24>
   ```
2. **Address.** `SITE_ADDRESS=rag.example.com` for a real domain (see *TLS* below), or
   leave `localhost` to try the stack.
3. **Models.** See *Choosing models* below.

Start it:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
docker compose --env-file .env.prod -f docker-compose.prod.yml ps
```

Compose starts things in dependency order: Postgres and Redis become healthy, `migrate`
runs and exits, then the app and worker start, and Caddy starts last, once the app
reports healthy. Then create a tenant and its key:

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml exec app \
  ragbridge-admin create-tenant --name acme
```

The key is printed once and stored only as a hash; save it. Try it:

```bash
curl -k https://localhost/health/ready                       # {"status":"ok"}
curl -k -H "Authorization: Bearer <key>" https://localhost/documents   # []
```

(`-k` is needed only with the default `localhost` address, whose certificate comes from
Caddy's own local CA.) A walkthrough with real output is in [demo.md](demo.md).

If ports 80 or 443 are taken on the machine, set `HTTP_PORT` and `HTTPS_PORT` in
`.env.prod`.

## TLS and a domain

With `SITE_ADDRESS=rag.example.com`, Caddy obtains and renews a Let's Encrypt
certificate by itself. It needs a DNS `A` record pointing at the server, and ports 80
and 443 reachable from the internet. **This path has not been run** - there was no
domain to test with. With `SITE_ADDRESS=localhost` (the default), Caddy issues a
certificate from its own local CA; that path was run, and clients will not trust it
without extra steps.

## Choosing models

Defaults use an Ollama running directly on the host (`OLLAMA_BASE_URL` defaults to
`http://host.docker.internal:11434`, which the Compose file maps to the host). Two
things to know:

- **On Linux, Ollama listens on `127.0.0.1` by default, which containers cannot
  reach.** It has to listen on an address the Docker bridge can reach (for example
  `OLLAMA_HOST=0.0.0.0`), and if so, **port 11434 must be firewalled from the
  internet.** This is from Ollama's documented behaviour and was not tested on Linux.
- **A hosted provider needs no Ollama at all.** Set `CHAT_MODEL` (and optionally
  `EMBEDDING_MODEL`, `AGENT_PLANNER_MODEL`) to a
  [LiteLLM model name](https://docs.litellm.ai/docs/providers) and add the provider's
  API key to `.env.prod` as an environment variable. **No hosted provider was tested
  here** - only local Ollama.

`EMBEDDING_MODEL` and `EMBEDDING_DIMENSION` are fixed per installation: changing either
after documents exist means a new migration and re-embedding everything.

## Day-to-day

```bash
dc() { docker compose --env-file .env.prod -f docker-compose.prod.yml "$@"; }
dc ps                    # state and health of every service
dc logs -f app           # follow one service's logs
dc restart app           # restart one service
```

Container logs are rotated (10 MB x 3 files per container), so they cannot fill the disk.
Postgres, Redis, the app and the worker have healthchecks (Caddy and the one-shot
`migrate` do not). The worker's check reads a heartbeat the worker writes every 30
seconds: after a crash the check fails within about 35 seconds, and Docker marks the
container unhealthy after two failed checks in a row, 30 seconds apart.

## Backups and restore

Back up the database - it holds documents, chunks, embeddings, tenants and key hashes.
Redis holds a queue and a cache: losing it costs the cache and any large uploads still
waiting to be processed (re-upload them), so it is not backed up here.

```bash
dc exec -T postgres pg_dump -U ragbridge -d ragbridge -Fc > backup-$(date +%F).dump
```

**Store the dump off the server.** A backup on the same disk does not survive the
disk. To restore into a fresh stack (started as in *First deployment*, so migrations
have created the schema):

```bash
dc exec -T postgres pg_restore -U ragbridge -d ragbridge --clean --if-exists --no-owner \
  < backup-2026-09-19.dump
```

This exact sequence was run: a dump was taken of a stack with 60 documents, **every
volume was destroyed**, a fresh stack came up (the old API key was then rejected with
401, confirming the data was gone), the dump was restored, and afterwards the **same
key** saw all 60 documents and search returned the right chunks. The dump was 240 KB.
The cache is empty after a restore and refills as you use it.

## Upgrading

```bash
git pull
dc up -d --build
```

The image is rebuilt, and `migrate` runs again before the app is replaced. This was run
with a code change: the migrate container started
again with the new image and exited 0, then the app was recreated and readiness
returned 200. **It has not been run with a release that changes the database schema**
(none has existed since this file was written), so take a backup first.

Expect a short interruption while the app container is replaced; there is a single app
replica.

## Security notes

What the stack already does, each checked by `scripts/smoke-prod.sh` (21 checks, run in
CI):

- Only Caddy publishes ports; Postgres and Redis are unreachable from outside.
- The secrets have no defaults, and the app refuses to start with the database
  credentials published in `.env.example`.
- Redis requires its password.
- The app and worker run as an unprivileged user, from code they cannot modify.
- `/docs` and `/openapi.json` are off.
- A request body over `MAX_BODY` (12 MB) is refused by the proxy before the app reads it.
- No private or developer files are in the image.
- `/documents`, `/query`, `/search`, `/agent` and `/mcp` reject a missing key.

What is up to you:

- **Firewall.** Allow only SSH, 80 and 443. Docker publishes container ports itself and
  can bypass host firewall rules such as `ufw`, which is why the stack publishes nothing
  but Caddy's ports; do not add `ports:` to other services. (Known Docker behaviour,
  not tested here.)
- **`.env.prod`** holds secrets: keep it out of git (it is ignored) and out of backups
  you share.
- **API keys.** Create and revoke with `ragbridge-admin` (see the README). A lost key
  cannot be recovered, only revoked and replaced.
- **Rate limiting.** There is none - see below.
- Keep the operating system and Docker updated.

## Known limitations

- **No rate limiting.** Any holder of a valid key can send requests as fast as the
  server accepts them, and each `/query` and `/agent` costs a model call. Put a limit in
  front of it (a cloud firewall or a proxy with a rate-limit module); plain Caddy does not
  include one.
- **One app replica.** Not designed or tested for several.
- **A model-provider outage surfaces as HTTP 500**, not a clear 502/503. Seen when the
  app could not reach Ollama.
- **`/agent` with the default planner is not meaningfully better than `/query`** on
  multi-step questions; see [evaluation.md](evaluation.md) and
  [ADR 0006](adr/0006-hand-rolled-agent-loop-instead-of-langgraph.md).
- **Redis has no memory cap.** Cached embeddings expire after a day by default, which
  bounds it, but this was not measured over time. Capping it safely needs care: evicting
  the wrong keys could drop queued jobs.
- **Docker treats `docker kill` as a manual stop** and does not restart the container.
  A genuine crash (an out-of-memory kill was tested) is restarted automatically.

## What was verified, and what was not

| Step | Status |
|---|---|
| Image builds and runs as a non-root user | **Run** (laptop, and CI on Ubuntu) |
| Compose refuses to start without secrets | **Run** |
| Start order: migrate, then app and worker, then Caddy | **Run** |
| HTTPS through Caddy on `localhost` | **Run** |
| Only Caddy publishes ports; Redis requires its password | **Run** |
| Upload, search, generated answer, background worker over the real stack | **Run** (local Ollama) |
| Oversized upload refused by the proxy | **Run** |
| Backup, destroy everything, restore | **Run** |
| Upgrade after a code change re-runs migrations | **Run** |
| Worker crash detected; out-of-memory crash restarted | **Run** |
| The 21-check smoke test | **Run**, on a laptop and on a GitHub Ubuntu runner |
| Upgrade with a schema-changing release | Not run |
| Let's Encrypt certificate for a real domain | **Not run** (no domain) |
| A hosted model provider | **Not run** (no key) |
| Ollama reachable from containers on Linux | **Not run** |
| Host firewall behaviour | **Not run** |
| Latency and memory on server hardware | **Not measured** |
| Anything at all on a real VPS | **Not run** |
