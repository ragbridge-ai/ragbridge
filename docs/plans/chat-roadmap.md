# Chat — roadmap

This document plans a chat product on top of ragbridge: a backend that holds conversations,
and an optional widget that any website can embed. It covers two repositories:

- `ragbridge-ai/ragbridge` (the core service) gets one new phase, Phase 7: access control,
  follow-up questions, and a refusal signal.
- `ragbridge-ai/ragbridge-chat` (new) holds the chat gateway and the widget.

`ragbridge-ai/ragbridge-php` gets token issuing, a token route, and template components.

## Why a separate repository

The core is an HTTP API for other applications, and AGENTS.md asks to keep its scope focused.
A chat product adds things the core should not own: end-user sessions, conversation storage,
per-tenant bot rules, a browser-facing surface with its own security rules, and later calls to
the host application's own API. Putting these in the core would make every core user carry
them. The [Phase 6 plan](phase-6-ui.md) already said multi-turn conversations are "a real
feature with a real data model"; this is that feature.

The core changes stay small, additive and opt-in, and are useful without the gateway.

## Architecture

```
end user's browser
  └─ host page ──<script>──> widget loader
                               └─ iframe (served by the gateway)
                                    │  HTTPS, short-lived session token
                                    ▼
                              chat gateway  ── tenant API key ──>  ragbridge core
                                    │                               (retrieval, answer)
                                    └── later: allowlisted calls ──> host app REST API

host app backend ── signs session tokens for logged-in users (private key never leaves it)
```

Two kinds of end users:

| User | Who vouches for them | Access groups |
|---|---|---|
| Public visitor | nobody - the gateway issues an anonymous session itself | `public` only |
| Organization member | the host app, by signing a token after its own login | from the signed token |

## Decisions

1. **Document access is enforced in the core, not in the gateway.** Each document gets a list
   of access groups. A retrieval request can carry the caller's groups, and only chunks whose
   document shares at least one group are searched. Filtering after retrieval in the gateway
   was rejected: the model would already have seen the forbidden chunks, and the cache, the
   agent loop, `/search` and MCP would each need their own copy of the rule.
2. **A document with no access groups is visible to no end user** (fail closed). Server-side
   callers with a plain API key keep seeing everything, so existing installations do not
   change. To make a document public, it must be labelled `public` on purpose.
3. **An API key can be marked `requires_access_scope`.** The gateway's key has this flag, so a
   gateway request without `access_groups`, or with an empty list, is rejected (`403`) instead
   of silently searching everything. A bug in the gateway then fails loudly, not by leaking.
   Such a key is also rejected on every `/documents` endpoint, reads included: listing
   documents would show the titles of every group's documents, and the gateway never manages
   documents - the host app does that with its own plain key.
4. **The access scope is part of the answer cache key.** The current key hashes the tenant's
   corpus version, the settings, the question, `mode`, `top_k` and `explain`. Without the
   groups, a cached answer built from HR documents could be served to a public visitor. The
   groups are sorted before hashing so `["hr","it"]` and `["it","hr"]` share a key. A test
   proves both directions.
   The key alone is not enough: if a document leaves `hr`, an answer cached under `["hr"]`
   that used it would still be served until it expires. So any change to a document's
   `access_groups` bumps the tenant's corpus version, as a title change already does
   (`sync.py`). The change is also written to the document's existing chunks at once,
   including while a replacement text is `pending` - on that path the old chunks stay
   searchable until the worker swaps them, and must not keep the old groups meanwhile.
5. **The host app is the identity provider; the gateway never handles passwords or SSO.**
   The host app logs its users in as it already does, then signs a short session token with
   Ed25519. The gateway stores only the public key, so a database leak at the gateway cannot
   mint tokens. Ed25519 because PHP's `ext-sodium` signs it with no extra package, and Python
   verifies it through `PyJWT` with `cryptography`. HS256 was rejected: a shared secret would
   let the gateway mint tokens for any user of the host app.
6. **Token rules.** Claims: `iss` (tenant id), `aud` (`ragbridge-chat`), `sub`, `groups`,
   `iat`, `exp`, `jti`; `kid` in the header. Lifetime at most 15 minutes - the gateway rejects
   longer tokens even when the signature is valid. The token never appears in a URL; the
   loader passes it to the iframe with `postMessage` and an exact target origin.
7. **Public sessions are issued by the gateway itself**, only for tenants with public mode on,
   only with the group `public`, and only for an `Origin` on the tenant's allowlist. `Origin`
   can be faked outside a browser, so this does not protect data - public documents are
   public anyway. It limits who embeds the widget. Cost abuse is handled by rate limits and a
   daily budget per tenant (decision 14).
8. **Conversations belong to a `sub` inside a tenant.** A conversation is only readable with a
   token for the same tenant and `sub`. Every new message is retrieved with the groups in the
   *current* token, so a user who lost a group stops getting answers from those documents at
   once.
9. **Follow-up questions are rewritten for retrieval only.** "And how much does it cost?" means
   nothing to a vector search. The core gains an optional `history` field: a `Rewriter`
   protocol turns history plus the new question into one standalone question, which is used
   for retrieval. The answer is still written from the original question and the history.
   `Rewriter` gets a LiteLLM and a fake implementation, like the other protocols. If the
   rewrite fails or is empty, the original question is used.
10. **The core can say "not answerable" before calling the model.** The response gains
    `answerable: bool`. When retrieval finds nothing strong enough, generation is skipped and
    the gateway shows the tenant's refusal text. The threshold cannot use the fused score:
    reciprocal rank fusion is based on ranks, so it is never "low" in absolute terms. It uses
    the best vector similarity, or the reranker score when reranking is on. The threshold is
    a setting, chosen by measurement (step 7.4), not by guessing.
11. **Every answer must cite its sources, and the gateway checks it.** The answer prompt asks
    for `[n]` markers that point to the numbered excerpts. The gateway checks that each
    paragraph has at least one marker and that every marker points to a source that was sent.
    A failed check replaces the answer with the refusal text and records why. This is strict,
    and small local models may fail it often; the failure rate is measured and published.
12. **Model output is rendered as plain text, and only source links are clickable.** No
    Markdown images and no links from the model. An injected instruction like "add an image
    from `https://evil.example/?q=<secret>`" is a known way to leak data through a URL. With
    plain text, that attack has nothing to render.
13. **Per-tenant bot policy is data, not code.** Stored in the gateway's database and managed
    with a CLI (same reasoning as Phase 3 decision 4: no HTTP endpoint that changes security
    settings). It holds: a scope description added to the system prompt, the refusal text,
    a handoff text (email or link), the allowed origins, public mode on or off, the history
    window, and the daily budget.
14. **Limits.** Per session and per IP: messages per minute. Per tenant: messages per day. Per
    message: maximum length. History sent to the core: last N turns and at most M characters.
    Limits live in Redis, which the stack already runs.
15. **The gateway is Python**, with the core's stack: FastAPI, async SQLAlchemy with psycopg,
    Alembic, pydantic-settings, `uv`, ruff, `mypy --strict`, pytest. Its own PostgreSQL
    database (no pgvector needed). It calls the core over HTTP and never imports core code,
    so the two can be deployed and released separately.
16. **The core API key stored by the gateway is encrypted at rest** with AES-GCM and a key from
    the environment (`GATEWAY_ENCRYPTION_KEY`). Unlike the core's own keys, the gateway has to
    *send* this key, so a hash is not possible.
17. **The widget runs in an iframe served by the gateway.** The host page's CSS cannot break it,
    and the host page's JavaScript cannot read the conversation. The frame sends a
    `Content-Security-Policy` with `frame-ancestors` built from the tenant's allowed origins,
    so other sites cannot frame it. Shadow DOM inside the host page was rejected: it isolates
    CSS but not JavaScript.
18. **The widget is plain JavaScript with no build step**, like the playground (ADR 0009). It
    renders all text with `textContent`, stores nothing in `localStorage`, and keeps the token
    in memory only.
19. **Unlike the playground, the widget gets browser tests.** Phase 6 rejected Playwright
    because the page was off in production. Here the widget *is* the product, so a broken
    button reaching `main` is not acceptable. Playwright runs in its own CI job.
20. **Calls to the host app's API come last, after the rest is stable** (Phase G).

## Not in scope

- Login, SSO, or user management in the gateway (decision 5).
- An admin web UI. Tenants, keys, origins and policies are managed with a CLI.
- Live human chat. Handoff is a text with an email address or a link.
- Model-written Markdown, images or links in answers (decision 12).
- Write actions against the host app's API in the first version of Phase G.
- A WordPress plugin. Planned after the PHP template components.

## Data model (gateway)

- `tenants`: `id`, `name`, `core_base_url`, `core_api_key_encrypted`, `public_mode`,
  `created_at`
- `tenant_signing_keys`: `id`, `tenant_id`, `kid` (unique per tenant), `public_key`,
  `created_at`, `revoked_at`
- `tenant_origins`: `tenant_id`, `origin`
- `tenant_policies`: `tenant_id`, `scope_text`, `refusal_text`, `handoff_text`,
  `history_turns`, `history_max_chars`, `daily_message_limit`
- `conversations`: `id`, `tenant_id`, `subject`, `created_at`, `last_activity_at`
- `messages`: `id`, `conversation_id`, `role` (`user` | `assistant`), `content`, `sources`
  (`jsonb`), `outcome` (`answered` | `not_answerable` | `citation_check_failed` |
  `rate_limited`), `created_at`
- `feedback`: `message_id`, `rating` (`up` | `down`), `comment`, `created_at`
- Retention: conversations older than `CONVERSATION_RETENTION_DAYS` are deleted by a
  scheduled job.

## Data model changes (core)

- `documents.access_groups`: `text[]`, not null, default `{}`, GIN index.
- `chunks.access_groups`: a denormalised copy, for the same reason `tenant_id` is copied in
  Phase 3 decision 3 - a filter on `chunks` itself, which `hnsw.iterative_scan` (already set
  in `vector_search`) applies while it walks the index, so a restrictive group does not
  usually return three chunks out of twenty. Not always: the walk stops at
  `hnsw.max_scan_tuples` (20 000 by default), so a very small group in a large tenant can
  still get fewer candidates than asked for.
- `api_keys.requires_access_scope`: `bool`, default `false`.

## Demo stack

One `docker compose` file in `ragbridge-chat` that starts the core, the gateway, Redis,
both databases, and a small demo site with two parts:

- a public shop page with the widget, answering from documents labelled `public`;
- an intranet page with a fake login for two demo users in different groups (for example
  `hr` and `engineering`), each answering from their own documents.

The same question asked on the three pages must give three different answers. Every step
below ends with a short "what you see in the browser" check on this site.

## Phases and steps

Each step is one feature branch of several small commits, as in earlier phases.

### Phase 7 — core: access control and conversation support (`ragbridge` repo)

The core's phases are numbered; the letter phases below belong to the other repositories.

- **7.1 Access groups on documents.** Migration; `access_groups` on upload (`POST /documents`,
  a multipart form field), on `PUT /documents/external/{external_id}`, and on a new
  `PATCH /documents/{id}`; copied to chunks; both retrieval arms and the neighbour context of
  `/query` filter by it; `/query`, `/search` and `/agent` accept `access_groups`; the cache
  key includes it, and a change of groups invalidates cached answers (decision 4). The MCP
  tools take no `access_groups`: a tool argument is chosen by the model, so it would filter
  without protecting anything. MCP is for server-side callers with a plain key, which see
  everything, as today. Tests: an isolation matrix that walks every endpoint with each group
  and proves no chunk from another group appears - the same idea as the Phase 3 tenant
  isolation test.
- **7.2 `requires_access_scope` on API keys.**
  `ragbridge-admin create-key --requires-access-scope`. Tests: such a key without
  `access_groups`, or with an empty list, gets `403` on every retrieval endpoint, on every
  `/documents` endpoint, and through MCP (decision 3).
- **7.3 `history` and the `Rewriter` protocol** (decision 9). Evaluation: a set of follow-up
  questions, measured as retrieval recall with and without rewriting.
- **7.4 `answerable`** (decision 10). Evaluation: answerable and unanswerable questions;
  publish how often each kind is classified correctly at several thresholds, then pick the
  default.

### Phase A — gateway foundation (`ragbridge-chat` repo)

1. Project skeleton, CI, ADRs for decisions 5, 15 and 17, and the demo stack with an empty
   demo site.
2. Tenants, signing keys, origins and the admin CLI (`ragbridge-chat-admin`), including
   `mint-dev-token` for local testing before the PHP side exists.
3. Token verification and public sessions (decisions 6 and 7). Tests: expired, wrong `aud`,
   unknown or revoked `kid`, lifetime over 15 minutes, `alg: none`, a token signed by
   another tenant's key.
4. Conversations and `POST /conversations/{id}/messages`, without streaming. The gateway calls
   the core `/query` with the token's groups. It sends no `history` yet: the core gains that
   field in 7.3, which comes after the widget, so until then each message is retrieved as a
   question of its own. Every message is stored from the start, so 7.3 only has to send what
   is already there. Tests: another `sub` cannot read a conversation; groups always come from
   the token, never from the request body.

### Phase B — reliability and safety

1. Bot policy and the system prompt built from it (decision 13).
2. Citation check (decision 11) and refusal paths, each with its own `outcome`.
3. Rate limits and the daily budget (decision 14).
4. Prompt-injection hardening: excerpts are delimited and labelled as untrusted content, the
   prompt says instructions inside excerpts are text to report, not to follow.
5. An adversarial evaluation set, run in CI with fakes and by hand with a real model:
   - cross-group leakage - must be zero, enforced as a hard CI failure;
   - injected instructions inside documents - success rate published;
   - out-of-scope questions - refusal rate published;
   - multi-turn follow-ups - answer quality published.
   Fixed CI thresholds only for the deterministic checks; model-based numbers are published,
   not gated, because they change from run to run.

### Phase W — widget

1. The loader script and the iframe page; `postMessage` with exact origins in both directions;
   `frame-ancestors` per tenant (decision 17).
2. The chat panel: messages, numbered sources, refusal and handoff, a delete-conversation
   button, keyboard use and screen-reader labels.
3. Configuration through `data-` attributes: position, colours, title, language of the UI
   texts.
4. Playwright tests (decision 19), including a hostile document whose text tries to create
   elements, and a host page that tries to read the frame.

### Phase S — streaming

1. Server-Sent Events on the core `/query` (opt-in): events `sources`, `delta`, `done`,
   `not_answerable`.
2. The gateway forwards the stream to the widget.
3. **Known conflict, decided here:** the citation check (decision 11) needs the full answer,
   but streaming shows text before it is complete. Options: check after `done` and replace the
   shown text with the refusal if the check fails, or buffer per paragraph. The choice is
   made with the failure rate from Phase B in hand.

### Phase P — PHP integration (`ragbridge-php` repo)

1. `access_groups` on document methods, and a resolver in the queued sync so the application
   decides each record's groups.
2. `ChatToken::issue(subject, groups)` with `ext-sodium`, and a key-pair console command.
3. A token route for Laravel and Symfony that the widget calls to refresh its token.
4. A Blade component and a Twig function that print the widget tag.

### Phase O — operations

1. Tracing through Langfuse with message text redacted by default.
2. Feedback (`up` / `down`) stored and exportable.
3. Retention job and a documented backup and restore.

### Phase G — calls to the host app's API (after the system is stable)

1. A per-tenant tool manifest: a small subset of OpenAPI, each operation with a JSON Schema for
   its arguments, read-only in this version.
2. The model only proposes a call; the gateway validates the arguments, adds the user's
   identity from the token (never from model output), calls the host API with a timeout and a
   response size limit, and gives the result back to the model as untrusted content.
3. Evaluation: injected instructions that try to trigger a call; the published rate must be
   zero for calls the user did not ask for.
4. Write operations, each needing an explicit confirmation from the user in the widget, are a
   later step with its own plan.

## Order

`7.1 → 7.2 → A → B → W → 7.3 → 7.4 → S → P → O → G`

7.1 and 7.2 come first because nothing for organization users may be built on a core that
cannot separate their documents; a minor core release follows 7.2. 7.3 and 7.4 move after the
widget, so their effect can be seen in the browser, not only in numbers. S1 is in the core,
S2 and S3 in `ragbridge-chat`. Phase G starts only once the system is declared stable.

## Step 7.1 detail — access groups on documents

Branch: `feat/phase-7-1-access-groups` (created from `main`, off the merged roadmap).
This step labels documents with access groups and makes every read of chunks honour them.
It does **not** make any key require a scope: a request without `access_groups` sees
everything, exactly as today. That is step 7.2. Nothing changes for an existing caller.

**Decided after reading the code** (each one a gap the plan above did not settle):

1. **A re-upload of identical bytes with different groups is a `409`.** `POST /documents`
   returns the existing document when the same file is uploaded again
   (`api/documents.py`, the `sha256` check), and would silently drop the new groups. A
   re-upload that sends no `access_groups`, or the same set, keeps today's `200`. The
   `409` says to use `PATCH`.
2. **`PUT /documents/external/{id}` without `access_groups` keeps the stored groups**
   (`{}` for a new document); a list, even an empty one, replaces them. `PUT` is a
   full-state write for text, title and metadata, but a client that does not know the
   field, or a metadata-only sync, must not hide a document by wiping its groups. A change
   of groups alone reports `result: "updated"`.
3. **`access_groups` missing and `access_groups: []` are different requests.** Missing:
   no filter (a server-side caller, as today). Empty: a caller with no groups, which
   matches no document. The two hash to different answer-cache keys.
4. **The keyword arm's word statistics are scoped too.** `_term_stats` counts how many
   chunks contain each query word, to drop common words (`KEYWORD_MAX_TERM_FREQUENCY`) and
   to weight rare ones (`KEYWORD_RARITY_WEIGHTING`). Counted over the whole tenant, chunks
   the caller cannot see would shape its ranking. So five reads of `chunks` filter by
   group, not four: the vector arm, both keyword queries, the statistics, and the
   neighbours.
5. **Chunks take their groups at the moment they are stored, from the locked row.** The
   background worker embeds before it takes the row lock (`worker.py`), so groups copied
   in `build_chunks` could be a `PATCH` out of date by the time the chunks are swapped in.
   `store_chunks` runs under the lock and sets them from the document as it is then.
6. **Group names are validated like external ids:** `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`,
   at most 32 per document or request, stored de-duplicated and sorted. A change means a
   different set, not a different order.
7. **`chunks.access_groups` gets a GIN index too**, not only `documents`: every filter is
   on `chunks`, never through the join. Whether the planner uses it is measured, not
   assumed, and reported in the Outcome.

### Data model changes

- `documents.access_groups`: `text[]`, not null, server default `'{}'`, GIN index.
- `chunks.access_groups`: the same, a copy of its document's, GIN index.
- One migration, after `5e2b7c1d9a34`. Existing rows get `{}`: no end user sees them
  (decision 2), and plain keys see them as before. No backfill is needed, because every
  existing document's groups are `{}` too.

### API changes

Additive. No existing field changes, and no default response shape changes except one new
field on documents.

| Endpoint | Change |
|---|---|
| `POST /search`, `POST /query`, `POST /agent` | Optional `access_groups: list[str]`. Missing: no filter. |
| `POST /documents` | Optional repeated form field `access_groups`. `409` for a re-upload with a different set. |
| `PUT /documents/external/{external_id}` | Optional `access_groups` in the body; missing keeps the stored set. |
| `PATCH /documents/{id}` | New. Body `{"access_groups": [...]}`, required. `404` for another tenant's id. |
| Every response with a `DocumentOut` | Gains `access_groups`. |
| MCP tools, `RagbridgeClient` | None (step 7.1 above). |

No new settings.

### Proposed commit breakdown

1. **`docs: add Phase 7 step 1 plan`** (this section).
2. **`feat(db): add access_groups to documents and chunks`**
   - The two columns and their GIN indexes on the models, and the migration.
   - `store_chunks` sets each chunk's groups from the document it was handed, which on
     the worker path is the locked row (point 5). `build_chunks` does not set them.
   - Nothing reads the columns yet, and they are `{}` everywhere, so every existing test
     passes unchanged.
   - Tests: the migration creates both columns and both indexes; a document and a chunk
     round-trip their groups; the worker stores the groups the row holds when it takes
     the lock, not the ones it had when embedding started (a fake embedder changes the
     row's groups mid-embedding).
3. **`feat(retrieval): filter retrieval by access groups`**
   - New `src/ragbridge/access.py`: the validated group-name type and
     `normalise_groups` (point 6), shared by retrieval, the endpoints and the document
     writes.
   - `access_groups: Sequence[str] | None = None` on `vector_search`, `keyword_search`
     (and through it the two keyword queries and `_term_stats`),
     `hybrid_search_with_provenance`, `hybrid_search` and `fetch_neighbours`. `None` adds
     no condition; a list adds `chunks.access_groups && :groups`, so an empty list
     matches nothing.
   - Tests: each of the five reads returns only overlapping chunks, returns everything
     for `None` and nothing for `[]`; neighbours are filtered even when their own groups
     differ from the retrieved chunk's (set by hand, since a real document's chunks
     share one set - the filter is a second layer); a word that is common only in a
     hidden group is not dropped from a visible caller's query. **The existing retrieval
     tests are not edited**: that is the proof the default is unchanged.
4. **`feat(api): accept access_groups on /search, /query and /agent`**
   - The request field, validated (point 6), passed to retrieval and, for `/query`, to
     the neighbours.
   - The answer-cache digest gains the normalised groups, with a distinct marker for a
     missing field (point 3).
   - Tests: each endpoint honours the field; `422` for an invalid name or more than 32;
     `["hr","it"]` and `["it","hr"]` share a cache entry; an answer cached for `["hr"]`
     is not served to `["public"]`, nor the reverse; missing and `[]` are not served each
     other's answers.
5. **`feat(api): set access groups when writing documents`**
   - `DocumentOut.access_groups`.
   - `POST /documents`: the form field, and the `409` of point 1.
   - `PATCH /documents/{id}`: locks the row, writes the document's and its chunks' groups
     in one transaction, and bumps the corpus version only when the set changed.
   - `PUT /documents/external/{id}`: point 2. A change of groups is written to the
     existing chunks at once, also while new text is `pending` (decision 4), through the
     same function `PATCH` uses.
   - Tests: upload with groups stores them on the document and every chunk; the `409`
     and both `200` cases of point 1; `PATCH` changes chunks too, `404` across tenants,
     no cache bump for the same set in another order; `PUT` keeps groups when the field
     is missing, replaces them when sent, reports `updated` for a groups-only change; a
     groups change during a pending replace reaches the old chunks immediately.
6. **`test: add an access-group isolation matrix`**
   - New `tests/test_access_isolation.py`, the counterpart of
     `test_tenant_isolation.py`. One tenant, documents labelled `public`, `hr`,
     `engineering`, both, and none. For each scope (`["public"]`, `["hr"]`,
     `["engineering"]`, `["hr","engineering"]`, `[]`, missing) it walks `/search`,
     `/query` (with the answer cache off and on, neighbours on) and `/agent`, and asserts
     that every returned document shares a group with the scope; that missing sees all;
     that `[]` sees none; and that the unlabelled document is never seen with any list.
     Plus one sequence test: cache an answer for `["hr"]`, `PATCH` the document out of
     `hr`, ask again, and get no source from it.
   - Each protection is mutation-checked once by hand (remove the filter, see the matrix
     fail), and the Outcome says which ones were.
7. **`docs: document access groups`**
   - README and `docs/api.md`: how to label a document, how a caller scopes a request,
     and that a document with no groups is invisible to every scoped request.

Measured in this step, and reported in the Outcome with real numbers: whether the planner
uses the `chunks` GIN index in each arm (`EXPLAIN ANALYZE` on a generated tenant), and
whether a small group in a large tenant still gets its full candidate count under
`hnsw.iterative_scan` (the `max_scan_tuples` limit named under *Data model changes
(core)*).

Not in this step: `requires_access_scope` and every `403` (7.2), MCP, and the PHP client
(P1).

### Step 7.1 outcome

Written after the step, so the plan above stays as it was decided and this records where
building it disagreed.

**Where reality differed from the plan**

- The corpus-version bump for a changed group set happens **after** the commit, not before.
  The first version bumped inside `set_access_groups`, ahead of the commit, which leaves a
  window in which a query reads the new version but still reads the old groups, and caches
  an answer built from them under the new version, where it stays until it expires. It was
  found by reading the code, not by a test. `set_access_groups` now only writes and says
  whether anything changed; `PATCH` and `PUT` bump once the transaction is committed
  (`bump_corpus_version`). A test records, at every bump, the groups another connection
  sees, and fails when the bump moves back before the commit - each of the two paths was
  mutation-checked.
- Each protection was broken once by hand, to see that a test notices. Three tests were
  missing at first, and added:
  - the corpus-version order for `PATCH` (the test recorded the groups at the bump but did
    not assert them for `PATCH`);
  - the answer-cache key, which the isolation matrix could not see, because every matrix test
    starts from an empty cache and asks one scope; a test now asks every scope in turn on one
    corpus;
  - the surrounding context of `/query`, whose scope can only be seen when a chunk next to a
    visible one carries another group. A document's chunks normally share its groups, so
    that test labels the middle chunk by hand.
- What breaks which level of test: the direct tests (`test_retrieval_access.py`) catch all
  five reads of chunks (the vector arm, the keyword query, the OR fallback, the word
  statistics and the neighbours); `test_api_access.py` catches the cache key, sorting and
  de-duplication, `None` and `[]` sharing a key, the agent and the context ignoring the
  field; and the matrix catches the vector arm, the keyword query and the three endpoints
  not passing the scope. The matrix **cannot** reach three reads, and says so: the word
  statistics and the rarity fallback need at least 20 chunks in a tenant
  (`MIN_CHUNKS_FOR_TERM_FREQUENCY`), and a neighbour of a visible chunk carries the same
  groups. Only the direct tests cover them.
- The `PUT` flow gained no new result: a change of groups on its own is `updated`, and a
  `stale` write changes nothing, groups included.
- One existing test changed. `test_the_external_id_migration_downgrades_only_without_synced_documents`
  stepped back with `downgrade -1`, which now means this step's migration. It names its
  target revision (`c39ac7386406`) instead; its assertions are the same. It is not a
  security or isolation test.
- `tests/helpers.py` gained `add_document`, `new_tenant` and `tenant_id_for_key`, shared by
  the three new test modules.

**Results**

781 tests, up from 646. No existing test other than the one above was edited, and every
retrieval test written before this step passes unchanged with `access_groups` left out,
which is the evidence that the default is a no-op.

Measured on a scratch database with 59,920 chunks of random 768-dimension vectors in one
tenant, in four groups: `big` 59,000 chunks, `small` 750 (1.3%), `tiny` 150 (0.25%) and
`rare` 20 (0.03%); `LIMIT 20`, one query vector, one run each, on a laptop. Times are
single runs and only meant to show orders of magnitude.

| Vector arm, what the planner picks by itself | Plan | Results | Time |
|---|---|---|---|
| no groups, or `big` | HNSW index scan | 20 | 1.2-1.3 ms |
| `small` | bitmap scan of `ix_chunks_access_groups`, exact sort | 20 | 5.6 ms |
| `tiny` | the same | 20 | 4.2 ms |
| `rare` | the same | 20 | 3.9 ms |

So the GIN index on `chunks.access_groups` is used where it matters: a small group is
answered exactly, with every chunk it has. A small group never reaches the HNSW index.

To see what the HNSW index does with a filter, the other indexes were dropped and sequential
scans switched off, so HNSW was the only way to answer (a first attempt only switched scans
off, and the planner used the `tenant_id` index instead, so it measured nothing):

| Group | `iterative_scan = relaxed_order` | `iterative_scan = off` |
|---|---|---|
| `big` | 20 results, 1.2 ms | 20 results, 1.1 ms |
| `small` | 20 results, 57 ms | **0** results |
| `tiny` | 20 results, 59 ms | **0** results |
| `rare` | **4** results, 72 ms | **0** results |

Without the iterative scan a filtered group comes back empty, which is Phase 3 decision 3
again, for groups this time. With it, the walk stops at `hnsw.max_scan_tuples` (20,000 by
default) and `rare`, whose chunks are 1 in 3,000, got 4 of its 20. Lowering the limit
shows the same effect directly: at 5,000 `small`, `tiny` and `rare` got 20, 13 and 1; at
1,000 they got 10, 2 and 0.

The keyword arm did not use `ix_chunks_access_groups` at all: with and without the index
the planner chose `ix_chunks_tenant_id`, in 6.7-8.2 ms, and returned the same rows (20, 15,
3 and 1 for the four groups, since only 1 chunk in 50 contains the query word). On this data
the new index helps the vector arm and not the keyword arm.

**Still not done - stated, not hidden**

- Nothing makes a key send groups. A caller that leaves `access_groups` out still sees every
  document of its tenant, so the groups are a filter, not an access check, until a key can
  be marked to require them.
- Real embeddings cluster; these were uniform random vectors, so how a small group fares
  under the iterative scan on real data is not known. Neither is how the plans change at
  much larger tenants: the planner chose the exact path for small groups here because the
  tenant is small enough for it.
- The cost of a `PATCH` was not measured. It rewrites every chunk of the document, and
  `access_groups` is indexed, so each rewritten row is inserted again in every index of
  `chunks`, the HNSW index included. That is the price of an in-place change without
  re-embedding, reasoned from the schema, not benchmarked.
- The migration was run on empty and small tables only. `CREATE INDEX` without
  `CONCURRENTLY` blocks writes to `chunks` while it builds, which is unmeasured on a large
  table; adding the column with a constant default does not rewrite it.
- A document whose new text is `pending` was tested with a queue that never runs the job,
  and the worker with the fake queue. No real Redis and `arq` worker run was repeated for
  this step.
- Group names are case-sensitive: `HR` and `hr` are two groups.

## Step 7.2 detail — API keys that require an access scope

Branch: `feat/phase-7-2-scoped-keys` (created from `main`, off the merged step 7.1).
Step 7.1 lets a request limit itself to some groups. This step lets a key be marked so that
it *must*: a request with such a key and no groups fails instead of seeing the whole
tenant. It turns the groups from a filter into an access check for the keys that carry the
flag. A plain key behaves exactly as before.

**Decided after reading the code:**

1. **Closed by default.** No endpoint can see its key today: `get_tenant` returns only the
   `Tenant` (`auth.py`). Rather than add a check to each of the eight `/documents`
   endpoints, `get_tenant` itself answers `403` for a key with `requires_access_scope`. Every
   endpoint that depends on it is then closed to such a key, including any added later. The
   three endpoints that accept a scoped key - `/search`, `/query` and `/agent` - depend
   instead on a new `get_caller`, which returns the tenant together with the key's flag, and
   check the request's `access_groups` against it. A forgotten check therefore fails closed,
   the same reasoning as Phase 3's router-level dependency.
2. **`401` before `403`.** A missing, unknown or revoked key is still `401` with the same
   message as before; only a valid scoped key reaches the `403`. The detail says what to
   change: `"this API key requires access_groups"` on the retrieval endpoints (for a missing
   field or `[]`), and `"this API key cannot manage documents"` on `/documents`.
3. **MCP gets a readable error.** A scoped key can never succeed through MCP, because the
   tools take no groups (step 7.1). `RagbridgeClient` turns every error but `401` into
   "ragbridge returned HTTP 403"; it gains a message for `403` that says the key requires
   `access_groups`, which MCP tools do not send. No tool's schema changes.
4. **The CLI only creates such keys.** `ragbridge-admin create-key --requires-access-scope`,
   which says so when it prints the key. `create-tenant` keeps creating a plain key, and an
   existing key cannot be changed: revoke it and create another. Listing keys is not part of
   this step.

### Data model changes

- `api_keys.requires_access_scope`: `bool`, not null, server default `false`. One migration
  after `b84f1c6a2d97`; every existing key stays a plain key.

### API changes

| Endpoint | With a key that requires an access scope |
|---|---|
| `POST /search`, `POST /query`, `POST /agent` | `403` without `access_groups` or with `[]`; otherwise as in step 7.1 |
| Every `/documents` endpoint, reads included | `403` |
| `/mcp` tools, `ragbridge-mcp` | A tool error saying the key requires `access_groups` |

Nothing changes for a plain key. No new settings.

### Proposed commit breakdown

1. **`docs: add Phase 7 step 2 plan`** (this section).
2. **`feat(db): add requires_access_scope to api_keys`**
   - The column on `ApiKey` and the migration. Nothing reads it yet.
   - Tests: a migrated database gives an existing key `false`; a key round-trips the flag.
3. **`feat(auth): require an access scope from keys marked for it`**
   - `auth.py`: a frozen `Caller` (`tenant`, `requires_access_scope`), `get_caller` (today's
     lookup, unchanged), `get_tenant` built on it and answering `403` for a scoped key, and
     `check_access_scope(caller, access_groups)` for the three retrieval endpoints, which
     move to `get_caller`.
   - Tests (`tests/test_scoped_keys.py`): each retrieval endpoint answers `403` to a scoped
     key without the field and with `[]`, and filters as in step 7.1 with a list; each of the
     eight `/documents` endpoints answers `403` to a scoped key with an otherwise valid
     request, so the `403` is not a `422` in disguise; a plain key is unchanged on all of
     them; an unknown key is still `401`. And a guard test that walks every operation in
     the app's OpenAPI schema except the health checks, calls it with a scoped key and no
     groups, and requires a `403` - an endpoint added later without the check fails it.
4. **`feat(mcp): explain a rejected scoped key in the tool error`**
   - The `403` message in `RagbridgeClient`.
   - Tests: a scoped key through `/mcp` gets that message from each of the three tools.
5. **`feat(cli): add create-key --requires-access-scope`**
   - The option, and a line in the output saying the key requires `access_groups`.
   - Tests: the option stores the flag and the printed key then gets `403` on `/documents`
     and `200` on a scoped `/search`; without it the key is plain.
6. **`docs: document keys that require an access scope`**
   - The API guide's access-group section: how to create such a key, what it can and cannot
     call, and the replacement of "nothing yet forces a key to send groups". The CLI list in
     *Multi-tenancy and API keys*. README, one line.

Each protection is broken once by hand to see that a test notices, as in step 7.1, and the
Outcome says which.

Not in this step: a command that lists keys, changing the flag of an existing key, and any
gateway code (Phase A).

### Step 7.2 outcome

Written after the step, so the plan above stays as it was decided and this records where
building it disagreed.

**Where reality differed from the plan**

- The route-walking guard could not send an empty request. The `/documents` endpoints are
  turned away by authentication before their request is read, so they are called with
  nothing. The three retrieval endpoints check the groups *inside* the endpoint, after the
  body has been validated, so with no body they answered `422`, not `403`, and the guard
  failed on its first run. It now sends those three a minimal valid body with no groups
  (`RETRIEVAL` in `tests/test_scoped_keys.py`). The consequence is intended: a new endpoint
  that takes a body answers `422` in the guard until its author adds it there, which is
  the moment to decide what a scoped key may do with it.
- A migration test written in step 7.1 stopped testing what it said it tested. It ended
  with `downgrade -1`, which after this migration means *this* step's migration, so it kept
  passing while no longer exercising the 7.1 downgrade at all. It was found while writing
  this step's own migration test, and both now name their target revision. Breaking the 7.1
  downgrade by hand fails the test again.
- The `403` for MCP is one fixed sentence (the key requires `access_groups`, which MCP tools
  do not send), for all three tools, not the server's own message: `list_documents` is
  refused with "cannot manage documents", which would be misleading there. The only `403` the
  REST API sends is for a scoped key, which the client states in a comment.
- `tests/helpers.py` gained `create_api_key`, used by the scoped-key, MCP and admin tests.

**Results**

811 tests, up from 781. No existing test other than the migration test above was edited,
and every test written before this step passes with plain keys unchanged: the default is a
no-op.

Each protection was broken once by hand, and a test noticed every time:

| Broken | Tests that failed |
|---|---|
| `get_tenant` lets a scoped key through | 9 (every `/documents` endpoint, the guard) |
| `/search` skips `check_access_scope` | 3 |
| `/agent` skips `check_access_scope` | 3 |
| an empty list is accepted | 3 |
| the key's flag is ignored | 15 |
| no `403` message in the MCP client | 1 |
| `create-key` does not store the flag | 1 |

`get_caller` runs the same single query as `get_tenant` did and only reads one more column,
so authentication costs no extra round trip. It was not benchmarked.

**Still not done - stated, not hidden**

- A key that requires groups can name **any** groups. The flag stops a request from
  forgetting them, not from choosing them: the server that holds the key decides who may see
  what, and a leaked key of this kind sees whatever groups the thief names. That server takes
  the groups from a signed token, never from the user (decisions 5 and 6, Phase A).
- A key's flag cannot be changed or listed. `ragbridge-admin` has no command that shows
  which keys of a tenant require groups, and `list-tenants` still prints no keys at all, so
  the answer is in the database or in the operator's own notes. Adding one is small, and was
  left out because the step did not ask for it.
- The check for a retrieval request lives inside each of the three endpoints, not in a
  dependency, because it needs the request's body. A fourth endpoint that takes
  `access_groups` has to call `check_access_scope`, and the guard only notices that if it is
  also added to `RETRIEVAL`.
- Nothing here was run against a real server with the stdio MCP proxy (`ragbridge-mcp`)
  holding a scoped key; it uses the same client class as `/mcp`, which is tested.

## Step A1 detail — gateway skeleton, CI, ADRs and the demo stack

Repository: `ragbridge-ai/ragbridge-chat`, branch `feat/a1-skeleton` (from its `main`, which
holds only the licence). This section and the step's outcome live here, in the core's plan,
which the gateway repository links to rather than copies.

A1 builds the empty frame every later step fills in: a gateway that starts, reports its
health and reaches its own database, a CI that keeps it to the core's standards, the three
decisions that shape the repository written down as ADRs, and one command that starts the
whole demo. It has no tenants, no tokens and no chat yet.

**Decided after reading the code:**

1. **The demo stack builds the core from its release tag.** There is no published core
   image; the core's own compose file builds its local `Dockerfile`. The demo builds it
   straight from the repository at a tag (`build: https://github.com/ragbridge-ai/ragbridge.git#v1.4.0`),
   so it runs a released core that has access groups and scoped keys, needs no checkout of
   the core next to this one, and changes only when the tag in the file does. CI never builds
   it: the gateway's tests use no network and no core.
2. **The demo site is a small FastAPI app**, one file in `demo/site/`, run from the gateway's
   image. A static site would do for A1, but the intranet's fake login has to sign tokens on
   the server with an Ed25519 private key from A3 on, so it would be replaced two steps later.
   It never imports the gateway's code: it stands in for the host application, which talks to
   the gateway over HTTP like any other.
3. **Two PostgreSQL servers in the demo:** `pgvector` for the core, plain `postgres:17` for
   the gateway (decision 15: its own database, no pgvector). Neither publishes a port, so the
   demo can run next to the core's own compose stack without a clash.
4. **The core needs Ollama on the host**, exactly as in the core's own compose file
   (`nomic-embed-text` and `llama3.2`). A1 only needs the core to start; the demo README says
   what is needed before a question can be answered.

### Repository layout

```
ragbridge-chat/
├── .github/workflows/ci.yml
├── docs/adr/                 # 0001-0003
├── demo/site/app.py          # the stand-in host application
├── migrations/               # Alembic environment, no revisions yet
├── src/ragbridge_chat/
│   ├── __init__.py
│   ├── main.py               # create_app()
│   ├── config.py             # pydantic-settings
│   ├── api/health.py         # /health, /health/ready
│   └── db/                   # base, session
├── tests/
├── AGENTS.md  README.md  LICENSE  Dockerfile  docker-compose.yml
├── alembic.ini  pyproject.toml  uv.lock  .env.example  .gitignore
```

Package `ragbridge_chat`, distribution `ragbridge-chat`, Python 3.12, `uv`. Dependencies are
the ones decision 15 names: FastAPI, Uvicorn, pydantic-settings, async SQLAlchemy with
psycopg, Alembic; ruff, mypy, pytest and httpx for development. `PyJWT` and `cryptography`
arrive with the step that first needs them.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://ragbridge_chat:ragbridge_chat@localhost:5433/ragbridge_chat` | The gateway's own database |

### The demo stack

`docker compose up --build` in `ragbridge-chat` starts:

| Service | Image | Port on the host |
|---|---|---|
| `core-postgres` | `pgvector/pgvector:pg17` | none |
| `core-redis` | `redis:7-alpine` | none |
| `core` | built from the core at `v1.4.0` | 8001 |
| `gateway-postgres` | `postgres:17` | none |
| `gateway` | built from this repository; runs `alembic upgrade head`, then Uvicorn | 8080 |
| `demo-site` | the same image, running `demo/site/app.py` | 8090 |

The demo site serves three placeholder pages - the public shop, the intranet as the `hr`
user, the intranet as the `engineering` user - which later steps fill in.

### Proposed commit breakdown (in `ragbridge-chat`)

1. **`chore: scaffold the project`**
   - `pyproject.toml`, `uv.lock`, ruff and mypy settings as in the core, `.gitignore`,
     `.env.example`, `create_app()` and `GET /health`.
   - Tests: `/health` answers `{"status": "ok"}`; `create_app()` builds a fresh app each
     time; a guard that no module under `src/` imports the core (`ragbridge`, as opposed to
     `ragbridge_chat`): the gateway reaches the core over HTTP only.
2. **`feat(db): add the database session, readiness check and Alembic`**
   - `DATABASE_URL`, the async engine and session, `GET /health/ready` (`200` when the
     database answers, `503` when it does not), and an Alembic environment with no
     revisions.
   - Tests: ready against the real test database; `503` against an unreachable one;
     `alembic upgrade head` runs on an empty database. The test database is created by a
     session fixture, as in the core.
3. **`ci: run lint, types and tests`**
   - One job: `ruff check`, `ruff format --check`, `mypy`, `pytest`, against a `postgres:17`
     service. No network, no core.
4. **`feat(demo): add the demo stack and an empty demo site`**
   - `Dockerfile` (the core's: multi-stage, a pinned `uv`, an unprivileged user),
     `docker-compose.yml` as above, `demo/site/app.py` with the three placeholder pages.
   - Tests: each page answers `200` with its own title; the demo site imports nothing from
     `ragbridge_chat`.
5. **`docs: add AGENTS.md, the README and ADRs 0001-0003`**
   - `AGENTS.md` in the core's style, linking to this plan.
   - README: what the project is, how to run the tests, how to start the demo.
   - ADR 0001: the host application is the identity provider, with Ed25519 tokens
     (decision 5). ADR 0002: the gateway is a separate Python service that reaches the core
     only over HTTP (decision 15). ADR 0003: the widget runs in an iframe served by the
     gateway (decision 17).

**What you see in the browser** after this step: `http://localhost:8090` shows the three
placeholder pages, `http://localhost:8080/health/ready` answers `{"status": "ok"}`, and
`http://localhost:8001/health` answers from the core.

Not in this step: tenants, keys and the admin CLI (A2), tokens (A3), conversations (A4),
and any widget code (Phase W).
