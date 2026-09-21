# External ids — documents identified by the client application

This plans a feature that is not in the roadmap's phase list. It fits the project's one
goal (AGENTS.md section 1: "RAG ready for existing applications") more directly than
most of what came before it: an existing application already has its own records, with
its own ids, and it needs to keep ragbridge in step with them.

## Why this exists

Today a document is identified by a UUID that ragbridge chooses, and deduplicated by the
SHA-256 of its bytes. That works for "upload a file", and it does not work for "keep my
records in sync":

1. **The application has to store ragbridge's UUID** next to every record, and it has to
   write that UUID back after every upload. A lost write, a restored backup, or a second
   environment and the mapping is wrong for good.
2. **An edit is a new document.** Changed text has a new hash, so `POST /documents`
   creates a second document and the old one stays searchable until the application
   remembers to delete it by UUID.
3. **Retries are not safe.** A sync job that times out and retries cannot know whether
   the first attempt landed. It only works today because identical bytes happen to
   deduplicate, and any change to the text breaks that.

A Laravel observer or a WordPress `save_post` hook wants one call: "this is record
`post:42`, here is its current state". That call must be safe to repeat, safe to reorder
and safe to run twice at once.

## Where things stand

Read out of the code and, where it says so, verified by running it.

| Fact | Where / how verified |
|---|---|
| `documents.sha256` is **unique per tenant**, `UNIQUE (tenant_id, sha256)`, constraint name `uq_documents_tenant_id` | `db/models.py`, migration `3cb3c5c7dcce` |
| `POST /documents` returns the existing document (200) when the same bytes are uploaded again by the same tenant | `api/documents.py` |
| That lookup is a `SELECT` then an `INSERT`: two identical uploads at the same moment both miss, and the second `INSERT` fails on the constraint (an unhandled 500) | `api/documents.py`; no `IntegrityError` handling anywhere in `src/` |
| Documents have **no metadata column** and **no `updated_at`**; only chunks have `metadata` (the PDF page) | `db/models.py` |
| `filename` is what sources and the context label show: `SourceOut.filename`, `SearchHit.filename`, `[filename, chunks 2-4]` | `api/query.py`, `api/search.py`, `context.py` |
| Retrieval does **not** filter on `documents.status`; a chunk that exists is searchable | `retrieval.py` |
| Large content goes to the arq worker by size (`ASYNC_PROCESSING_THRESHOLD`); the job carries only the document id and reads `raw_content` from the row | `api/documents.py`, `worker.py`, `jobs.py` |
| Every change that makes chunks searchable bumps `corpus_version:{tenant}`, which is part of the answer cache key; so does a delete | `ingestion.py`, `api/documents.py`, `api/query.py` |
| **The naming convention would give a new `UNIQUE (tenant_id, external_id)` the same name as the existing constraint**, `uq_documents_tenant_id`, because `uq` names use only the first column | compiled both constraints with `NAMING_CONVENTION`: two `CONSTRAINT uq_documents_tenant_id` lines |
| In a path parameter, `post:42`, `post%3A42` and `user@example` reach the handler decoded (`post:42`); **`%2F` does not** (404, it is decoded to `/` before routing); `..` is removed by the client, but **`%2E%2E` reaches the handler as `..`** | FastAPI `TestClient` probe |

**Not verified.** Postgres was not running for this plan, so nothing below about
`INSERT ... ON CONFLICT` and row locks was run here. It is documented Postgres behaviour,
and step 9 proves it with a real concurrency test before anything is released.

## Decisions

1. **A nullable `documents.external_id`, unique per `(tenant_id, external_id)`.**
   `NULL` means "uploaded, not synced", which is every existing row, so nothing that
   exists today changes meaning. Postgres treats `NULL`s as distinct in a unique
   constraint, so any number of plain uploads can have no external id.
   *Unique per tenant, not globally,* for the same reason `sha256` is (Phase 3
   decision 3): two applications both having a `post:42` is normal, and a global
   constraint would tell one tenant that the other has that id.
   *The constraint gets an explicit name,* `uq_documents_tenant_id_external_id`, because
   the naming convention would otherwise produce a name that already exists (see *Where
   things stand*).

2. **Uniqueness of content moves from "every document" to "uploads only"; for synced
   documents the hash becomes a change detector.** This is the conflict the current
   constraint creates: two different records can have identical text. Two products with
   the same boilerplate description, a page and its translation that has not been
   translated yet, two empty-template posts. Under `UNIQUE (tenant_id, sha256)` the
   second `PUT` would fail, and there is no honest answer to give: rejecting it breaks
   the sync, and pointing both ids at one document means deleting one record deletes
   the other's text.
   So:
   - The existing constraint is replaced by a **partial unique index**,
     `UNIQUE (tenant_id, sha256) WHERE external_id IS NULL`. Plain uploads keep exactly
     today's behaviour, including the deduplicating 200.
   - Synced documents are unique by `(tenant_id, external_id)` only. Their `sha256` is
     kept, indexed through the partial index for uploads and not unique for them, and is
     used to decide whether a `PUT` changed the text (decision 6).
   - `POST /documents` deduplicates **only against uploads** (`external_id IS NULL`).
     Otherwise an upload could be answered with a synced document, which a later sync
     `PUT` or `DELETE` would then change or remove under the uploader.
   *Rejected: dropping content uniqueness altogether.* It would change `POST`
   behaviour that clients rely on, for no gain to the sync feature.

3. **`external_id` is 1-255 ASCII characters from `A-Z a-z 0-9 . _ : @ -`, and it must
   start with a letter or a digit.** As a pattern: `^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$`.
   It is compared exactly, and it is case-sensitive.
   - **No `/`.** Verified: an encoded slash is decoded before routing and the request
     404s. Accepting `/` would mean a `{external_id:path}` route, and then the id's
     meaning would depend on how every proxy between client and server treats `%2F`
     (Caddy, nginx and load balancers disagree). `post:42` or `wp_posts.42` say the
     same thing.
   - **Must start with a letter or digit**, so `.` and `..` are impossible. Verified that
     `%2E%2E` reaches the handler as `..`; a client or proxy that normalises the path
     would silently turn it into a different URL.
   - **ASCII only**, so two ids that look identical cannot differ by Unicode
     normalisation (an `é` stored as one code point or as two).
   - **255** is long enough for composite ids such as `shop-de:product:9f1c...` with a
     UUID inside, and short enough for a B-tree index entry.
   - **URL encoding:** every allowed character is legal unencoded in a path segment
     (RFC 3986 `pchar`), so a client does not have to encode anything. A client that
     encodes anyway still works: PHP's `rawurlencode('post:42')` gives `post%3A42`, which
     the server decodes to `post:42` (verified). Anything outside the pattern is a 422
     that names the rule.

4. **`PUT /documents/external/{external_id}` creates or replaces; it is idempotent.**
   The body is JSON, not a file upload:

   ```json
   {"title": "Refund policy", "content": "...", "metadata": {"lang": "en"},
    "source_updated_at": "2026-09-21T10:00:00Z"}
   ```

   - `title` (1-500 characters) is stored in the existing `filename` column. That column
     is already the document's display name in sources and in the context label, so a
     synced document shows up in answers with its title and no retrieval, context or
     MCP code has to change. *Rejected: a separate `title` column,* which would have to
     be threaded through every place that shows `filename`, or would be ignored by them.
   - `content` is text and must contain at least one non-whitespace character. To empty a
     record, delete it. It is stored with `content_type = "text/plain"`, and its size in
     UTF-8 bytes is checked against `MAX_UPLOAD_SIZE`, as an upload is.
   - `metadata` is an optional JSON object, stored in a new `documents.metadata` JSONB
     column (default `{}`) and returned by `GET`, at most 16 KB when serialised. It is
     **not** used in retrieval yet (see *Not in scope*).
   - `source_updated_at` is optional and must carry a timezone (a naive timestamp is a
     422; guessing a time zone is how ordering bugs start).
   - The response is `{"result": ..., "document": {...}}`. `result` is one of `created`,
     `replaced`, `updated` (title or metadata only), `unchanged` or `stale`. The status is
     201 for `created`, 202 when the content went to the worker (decision 9), and 200
     otherwise.
   *Why `PUT` and not `POST` with an id in the body:* `PUT` on a URL the client names is
   HTTP's own definition of an idempotent create-or-replace, and every HTTP client
   (Guzzle, Symfony HttpClient, Laravel's `Http`) retries it without special handling.

5. **`DELETE /documents/external/{external_id}` returns 204 also when nothing exists.**
   The client's intent is "make sure this is gone", and that is true either way. A 404
   would make every retry of a delete that already succeeded look like a failure.
   *Not a leak:* the existing `DELETE /documents/{id}` returns 404 to avoid confirming
   another tenant's UUID. Here the lookup is always scoped to the caller's tenant, and an
   identical 204 for "deleted" and "never existed" reveals nothing at all.
   It bumps the corpus version only when it deleted something.
   `GET /documents/external/{external_id}` returns the document, or 404.

6. **If the content hash is unchanged, only title and metadata are updated: no
   re-chunking, no re-embedding.** The hash is the SHA-256 of `content` in UTF-8. Title
   and metadata are not part of it: neither goes into chunks, because the context label
   reads the title from the document at query time. This is what makes a sync that
   re-sends every record cheap: most records are `unchanged` and cost one indexed read and
   no embedding call.
   The status of the stored document matters:
   - **`failed` is re-processed** even with the same hash: the previous attempt did not
     produce chunks, so "same text" is not "already done". Re-sending the record is how a
     client retries.
   - **`pending` or `processing` is left alone:** the same text is already on its way, so
     the answer is `unchanged` with the current status, and no second job is queued.
   A title change bumps the corpus version, because a cached answer shows the old title
   in its sources. A metadata-only change does not, since nothing in an answer reads it.
   *Known limitation, as for uploads today:* changing `CHUNK_SIZE` does not re-chunk a
   document whose text is unchanged.

7. **A write whose `source_updated_at` is older than the stored one is ignored and
   reported as `stale`, with a 200, not an error.** Sync queues deliver out of order. The
   stale write is not the client doing something wrong; the correct final state is
   already stored, and the response returns it so the client can see that.
   - **Equal timestamps are applied**, not ignored. They are almost always a retry of the
     same write, and for a real conflict at the same instant "last write wins" is the
     only rule that needs no extra state.
   - **A write without `source_updated_at` is always applied, and leaves the stored value
     as it was.** A client that does not send it has opted out of ordering. Keeping the
     stored value means a client that sends it on some code paths is still protected on
     those.
   - The check runs **under the row lock of decision 8**, so two writes cannot both pass
     it against the same old value.

8. **Concurrency is handled by the unique constraint and a row lock, not by
   `SELECT`-then-`INSERT`.** Two `PUT`s for the same id at the same time must never
   create two documents. That is guaranteed by `uq_documents_tenant_id_external_id`
   alone: whatever the code does, Postgres will not store a second row. The code only has
   to turn that guarantee into a correct answer instead of a 500:
   1. `INSERT ... ON CONFLICT (tenant_id, external_id) DO NOTHING RETURNING id`, which
      either creates the row (`created`) or does nothing. While another transaction holds
      an uncommitted insert of the same id, this waits for it instead of failing.
   2. Otherwise `SELECT ... FOR UPDATE` the existing row. Every later writer of the same
      id queues behind this lock, sees the committed result of the one before it, and
      decides `unchanged`, `updated`, `replaced` or `stale` against that.
   The lock covers one row, so writes to *different* ids never wait for each other. It is
   held while the synchronous path embeds, which is what `POST /documents` already does
   inside its transaction today.
   *Rejected: catch `IntegrityError` and retry.* It works, but it turns the normal case
   of "record exists" into an exception, and the retry loop still needs a lock to make
   the ordering check of decision 7 safe.
   *Rejected: an advisory lock on a hash of the id.* It adds a second locking mechanism
   the constraint already makes unnecessary.

9. **Large content uses the existing job flow, with the hash in the job, and the old
   version stays searchable until the new one is ready.** Content over
   `ASYNC_PROCESSING_THRESHOLD` is stored as `raw_content` (its UTF-8 bytes) with
   `status = "pending"` and handed to the worker, exactly like a large upload (Phase 3
   decision 6), and the response is 202. What is new:
   - **Replace is an atomic swap.** The worker deletes the old chunks and writes the new
     ones in one transaction, so a search sees either the old version or the new one,
     never half of each and never nothing. The synchronous path does the same in the
     request's transaction. Retrieval does not filter on status, so the old chunks keep
     answering while the document shows `pending`.
   - **The job carries the hash it was queued for,** `process_document(id, sha256)`. The
     worker locks the row, and if the row's hash has moved on, a newer `PUT` replaced the
     content after this job was queued, so the job does nothing. Only the newest version
     is embedded, and a burst of edits costs one embedding run, not one per edit. The
     parameter is optional, so a job queued by the previous release still runs.
   - **A failed job keeps the last good version searchable.** The worker rolls back its
     chunk swap, sets `status = "failed"` and `error`, and the old chunks remain.
     Re-sending the record retries it (decision 6).
   - Uploads through `POST /documents` do not change: they never replace anything, so
     their jobs keep passing no hash.

## Not in scope

- **Filtering search by `metadata`.** It is the obvious next feature, and it needs its own
  decisions: which keys, how to index them, and how a filter interacts with the HNSW
  index's approximate search (the reason `chunks.tenant_id` is denormalised, Phase 3
  decision 3). Metadata is stored now so the application does not have to re-send
  everything once filtering exists.
- **Tombstones.** After a `DELETE`, a delayed `PUT` for the same id with an older
  `source_updated_at` creates the document again, because nothing remembers the delete.
  The fix is a deleted-at record per id, and it is only worth its cost if a real client
  shows the problem.
- **Bulk sync** (many records in one request) and **listing by external id prefix.** The
  single-record calls must be right first. A PHP client can loop.
- **Files by external id** (a PDF sent to `PUT`). Synced records are text. A file that
  belongs to a record can still be uploaded with `POST /documents`.
- **Including the title in the embedded text.** It may help retrieval for short records,
  but it changes chunks for every synced document and has to be measured with the
  evaluation scripts first.
- **Fixing the `POST /documents` race** (two identical uploads at once, see *Where things
  stand*). Same shape of fix as decision 8, a separate change.
- **MCP tools and the playground** stay as they are. Synced documents are searched like
  any other document.

## Data model changes

One migration, on `documents` only:

| Change | Detail |
|---|---|
| add `external_id` | `VARCHAR(255) NULL` |
| add `metadata` | `JSONB NOT NULL DEFAULT '{}'` (Python attribute `metadata_`, as on `chunks`) |
| add `source_updated_at` | `TIMESTAMPTZ NULL` |
| add `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()`, set on every change |
| drop `uq_documents_tenant_id` | the old `UNIQUE (tenant_id, sha256)` |
| add `uq_documents_tenant_id_sha256_uploads` | unique index on `(tenant_id, sha256) WHERE external_id IS NULL` |
| add `uq_documents_tenant_id_external_id` | `UNIQUE (tenant_id, external_id)` |

Every existing row has `external_id IS NULL`, so the partial index accepts exactly the
rows the old constraint did, and no data is rewritten. **Downgrade** refuses to run while
any row has an `external_id`, since it cannot restore `UNIQUE (tenant_id, sha256)` over
synced documents that share text; the message says to delete them first.

The model gets explicit names for both constraints (decision 1). See
[ADR 0010](../adr/0010-external-document-ids.md).

## API changes

| Endpoint | Change |
|---|---|
| `PUT /documents/external/{external_id}` | new, decision 4 |
| `GET /documents/external/{external_id}` | new, 200 or 404 |
| `DELETE /documents/external/{external_id}` | new, always 204 |
| `POST /documents` | deduplicates against uploads only (decision 2); otherwise unchanged |
| `DocumentOut` (every documents endpoint) | gains `external_id` (`null` for uploads), `metadata`, `source_updated_at`, `updated_at`; additive |

No route conflicts: `/documents/{document_id}` matches a single path segment, and
`/documents/external/{external_id}` has two.

## New settings

None. Size limits reuse `MAX_UPLOAD_SIZE` and `ASYNC_PROCESSING_THRESHOLD`.

## Steps

One commit each, on one branch, `feat/external-ids`. Every step keeps ruff, mypy and the
full test suite green.

1. **`docs: add external ids plan`** (this document and ADR 0010).
2. **`feat(db): add external_id, metadata and timestamps to documents`**: migration and
   model, the two named constraints and the partial index. Tests: two synced documents with
   the same text can coexist, two uploads with the same text still cannot, the migration
   runs up and down on an empty table, and downgrade refuses with a synced row present.
3. **`fix(documents): deduplicate uploads only against other uploads`**: `POST` lookup
   filters `external_id IS NULL`; `DocumentOut` gains the new fields.
4. **`feat(documents): validate external ids and add GET by external id`**: the pattern of
   decision 3 as a path parameter, with tests for every rule (slash, `%2E%2E`, leading
   dot, non-ASCII, 255/256 characters, `post%3A42` decoding) and tenant isolation.
5. **`feat(documents): create documents with PUT by external id`**: the insert of decision
   8, synchronous path only, `created` with 201.
6. **`feat(documents): replace or update an existing external document`**: decision 6,
   `unchanged` / `updated` / `replaced`, the atomic chunk swap, the `failed` exception,
   and corpus-version bumps. Tests assert the embedder is **not** called for an unchanged
   or title-only write.
7. **`feat(documents): ignore out-of-order writes by source_updated_at`**: decision 7,
   including equal and missing timestamps.
8. **`feat(worker): process large external documents in the background`**: decision 9,
   the optional hash on the job, the superseded-job skip, and failure keeping the old
   chunks.
9. **`test(documents): prove concurrent PUTs create one document`**: two sessions against
   the real test database, started together with `asyncio.gather`, for a new id and for an
   existing one, asserting one row, and that the ordering check held.
10. **`feat(documents): delete by external id`**: decision 5.
11. **`docs: document syncing records by external id`**: `docs/api.md`, a README section
    with a Laravel-shaped example, and the limits of *Not in scope*.

## Outcome

Built as planned, in the steps above, with these differences found while building:

- **The MCP `list_documents` tool broke when `DocumentOut` grew.** It reused that model, so
  its output schema changed and the client failed to validate; the existing MCP tests caught
  it. The MCP server now has its own `DocumentSummary` with the original six fields, for the
  same reason `/search` has separate `Explainable*` models (ADR 0009).
- **A same-text write to a document that is still `pending` or `processing` updates the title
  and `metadata` if they differ** and reports `updated`; it is `unchanged` only when nothing
  differs. Decision 6 said `unchanged` in both cases.
- **The worker does not hold the row lock while it embeds.** Decision 9 said the worker locks
  the row and checks the hash. Holding a lock for minutes would make a `PUT` of the same id
  wait as long, so the worker embeds first and takes the lock only for the swap, where it checks
  the hash again. A write that lands while it embeds makes it discard its work (tested).
  `ingest_document` was split into `build_chunks` and `store_chunks` for this.
- **If the queue is down, the document is marked `failed` and the call answers 503.** Left
  `pending`, a client retry would be told `unchanged` and nothing would ever process it.
- **`ON CONFLICT DO NOTHING` already waits for a row that another transaction is updating**,
  so most concurrency tests would pass even without `SELECT ... FOR UPDATE`; that was checked by
  removing the lock. One test now takes only the lock, and fails without it. The lock closes the
  window before a writer's first `UPDATE`, which the ordering check depends on.
- **A burst of edits costs one *stored* version, not always one embedding run.** A superseded
  job that has not started yet skips itself in about 10 ms. One that is already embedding
  finishes, finds the hash has moved on at the swap, and discards its work, so a burst can cost
  one wasted embedding run. Decision 9 said "one embedding run"; it is "one stored version".
- **Removing `ON CONFLICT` fails the concurrency tests with a unique violation, and never
  creates a second row**: the constraint is the guarantee, as decision 8 says.

**Run for real, after the tests.** The branch's image was built and run beside the development
stack, with its own Postgres and Redis, the real arq worker and Ollama (`nomic-embed-text`,
`qwen2.5:7b`), and a copy of the development database at the previous migration head (4
documents, 70 chunks). Verified there:

- The migration ran on that copy: row counts unchanged, every `external_id` `NULL`, the old
  constraint replaced by the two new ones.
- Existing behaviour: plain upload 201, duplicate upload 200 with the same id, a 150 KB upload
  202 then `ready` from the worker, and searchable.
- `PUT` create 201 (60 ms), the same `PUT` again `unchanged` (10 ms), title change `updated`,
  new text `replaced` with the same document id, older `source_updated_at` `stale`. `/query`
  with the real chat model cited the synced document by its title.
- Ten simultaneous `PUT`s of one new id gave one document (one 201, nine 200); eight of
  different text gave one document with one set of chunks.
- A 150 KB replace answered 202 while the old version stayed searchable, then swapped after the
  worker. Three rapid 150 KB edits, three rounds: only the newest text was stored each time.
- `DELETE` 204, 204 again, 204 for an unknown id; other tenants' documents untouched.

The end-to-end script's own first run had one failing check, a flaw in the check (the keyword
search falls back to matching a word, so a search for a removed marker still found the new
text's chunk); the stored state was correct, and the check was rewritten to read the chunks.

**Not verified.** No real hosted embedding or chat provider, and nothing on a large production
table (the migration takes a lock while it builds the partial index). A rolling deploy where the
old worker meets a job with the new hash argument was not tried: deploy the app and the worker
together. The PHP example in the README is syntax-checked (`php -l`) but was not run against a
server or in a Laravel app.
