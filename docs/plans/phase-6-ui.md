# Phase 6 — A playground UI

This document plans a phase that is **not** in the roadmap, and that reverses an earlier
decision. [docs/plans/phase-5.md](phase-5.md) put a web UI under *Not in scope*: "The
project is an API service (AGENTS.md section 1); FastAPI's `/docs` is the interactive
surface. A UI is a feature of its own, not a release task."

That reasoning was right for a release phase, and the last sentence is the one this plan
acts on: a UI is a feature of its own, so it gets its own phase. The product is still the
API. What is added here is a **playground**: a page for trying the service on your own
files and for *seeing what retrieval did*. It is a development and demonstration tool, it
is off in production, and it is not a product surface.

Nothing about the API's purpose changes: no application is expected to embed this page.
Laravel, Symfony and WordPress apps keep talking HTTP, exactly as before.

## Why this exists now

Two problems, both observed rather than imagined:

1. **Trying the service requires curl.** Uploading a file, remembering a document id,
   quoting JSON in a shell, and pasting a bearer token on every call. `docs/demo.md` is a
   good scripted walkthrough, but a person evaluating this project has to retype it with
   their own files. "Ready-to-use RAG" and "first you will need a `curl -F`" sit badly
   together.
2. **There is no way to look at retrieval on your own documents.** `evaluation/` measures
   recall@k, MRR and RAGAS scores over a fixed six-document corpus. Those are aggregates
   over someone else's data. They do not answer the question that actually comes up: *this
   answer about my file is wrong - which chunks were found, what did they score, and did
   the agent search for something silly?* Phases 2 and 4 built hybrid retrieval, reranking
   and a bounded agent, and the only way to inspect any of it is to read JSON in a
   terminal.

Better documentation, on its own, fixes neither. That option was considered seriously and
rejected on point 2: no amount of prose shows you the chunks that were retrieved from
*your* PDF.

## Where things stand

Read out of the code and, where it says so, verified by running it.

| Fact | Where / how verified |
|---|---|
| `/`, `/ui` and `/favicon.ico` all return **404**; nothing is served but the API | `TestClient(create_app())` |
| No CORS middleware exists anywhere | grep over `src/` - no match |
| No rate limiting exists anywhere | grep over `src/` - no match |
| No static files, no template engine, no `jinja2` in `pyproject.toml` | grep + `pyproject.toml` |
| `fastapi.staticfiles.StaticFiles` works with **no new dependency** (starlette 1.6.0 is already installed) | mounted it in a probe: `/ui/` → 200, `/health` unaffected |
| A `StaticFiles` mount refuses encoded path traversal (`/ui/%2e%2e%2f%2e%2e%2f.env` → 404) | same probe |
| Under `Content-Security-Policy: script-src 'self'`, an **inline `<script>` does not run**; an external `/app.js` does | headless Chrome, `--dump-dom` |
| Under that same CSP, an `innerHTML` injection carrying `<img onerror=...>` **did not fire**; without CSP it **did** | headless Chrome, two ports |
| A page served from the same origin as the API can `fetch` it with an `Authorization` header and **no CORS headers at all**; the same code from a different origin is **blocked** | headless Chrome, two ports |
| Every endpoint except `/health` and `/health/ready` needs `Authorization: Bearer rb_...` | `auth.get_tenant` |
| There are **no admin HTTP endpoints**; keys are minted only by the `ragbridge-admin` CLI | `admin.py` is `argparse` only |
| `SearchResult` is a bare 3-tuple `(Chunk, Document, float)` used by the reranker `Protocol` (4 signatures), the agent loop (by index) and three endpoints | `retrieval.py`, `rerank.py`, `agent/loop.py` |
| `/query` and `/search` return **one fused score** per hit and nothing about which arm found it | `api/query.py`, `api/search.py` |
| The answer cache key hashes `question\|mode\|top_k` only | `api/query.py` |
| `docker-compose.prod.yml` forces `ENABLE_DOCS=false`; `scripts/smoke-prod.sh` asserts `/docs` is 404 (21 checks) | those files |

**Not verified.** No page has been built yet, so nothing is known about how this looks or
feels in practice. The browser checks above were run in headless Chrome only - not in
Firefox or Safari - and they test the mechanisms this plan depends on, not a real
playground.

## Decisions

1. **A playground, not a UI product.** One page for trying the service and inspecting
   retrieval. It is named `/playground`, not `/ui` or `/app`, because the name sets the
   expectation: a place to experiment, not the front end of anything.
   *What is given up, stated plainly:* this will never be an end-user interface for a
   tenant's own staff. Anyone who wants that builds it in their own application, which is
   what the API is for.

2. **Off in production, on in development - an explicit setting, not a guess from
   `ENVIRONMENT`.** A new `ENABLE_PLAYGROUND` (default `true`), set to `false` by
   `docker-compose.prod.yml`. This copies Phase 5 decision 6 (`ENABLE_DOCS`) deliberately:
   the same shape of switch, the same reasoning, one fewer thing to learn.
   *Why the default differs in effect from `ENABLE_DOCS`:* a public `/docs` leaks the API's
   shape. A public playground leaks nothing by itself - it holds no key and reads no data
   without one - but it is an unnecessary page with JavaScript on a production host, and
   "unnecessary" is the whole argument. An operator who wants it can set it back to `true`.

3. **Plain HTML, CSS and JavaScript, served by the app. No build step, no framework, no
   npm.** Three files: `index.html`, `playground.css`, `playground.js`, mounted with
   `StaticFiles`.
   *Why not a React/Vite SPA:* it means Node in the toolchain, a `package.json` and a lock
   file, a build step before the Python package can be built, a second CI job, and build
   output either committed or generated into the Docker image. AGENTS.md section 2 says to
   add a tool only when the current phase needs it. A page with roughly five interactions
   does not need a component framework, a bundler, or a virtual DOM. Verified: `StaticFiles`
   adds **no Python dependency either**, so this whole phase adds zero dependencies.
   *Where this breaks:* if the page ever grows tabs, routing and shared state, hand-written
   JavaScript stops being the simpler choice. That is the signal that the scope was
   exceeded, and the answer then is to stop growing the page, not to add a framework.

4. **Same origin, so no CORS middleware.** The page is served by the app, so the browser
   calls `/query` on the origin it was loaded from. Verified in a real browser: a
   same-origin `fetch` with an `Authorization` header succeeds with **no CORS headers
   present at all**, and the identical code from another origin is blocked.
   *Why this matters more than it looks:* the alternative - a page hosted anywhere else -
   forces a CORS middleware into a service that currently has none, and a CORS policy is a
   permanent security decision (which origins, credentials or not) that must then be
   configured correctly by every operator. Serving the page from the app deletes that
   entire problem instead of solving it.

5. **A strict Content-Security-Policy on the playground response, set by the
   application.** `default-src 'self'; script-src 'self'; style-src 'self'; connect-src
   'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors
   'none'`.
   Set by the app, not by Caddy: Caddy only runs in production, and this page mostly runs
   in development. A header that is present exactly where the page is is a header nobody
   can forget.
   *Consequence, verified rather than assumed:* under `script-src 'self'` an inline
   `<script>` **does not execute**, so the page cannot use inline scripts or `onclick=`
   attributes. All behaviour lives in `playground.js`, and events are attached with
   `addEventListener`. This is a real constraint on how the page is written, discovered by
   running it rather than after a confusing debugging session.

6. **Untrusted text is rendered with `textContent`. Never `innerHTML`.** Document text,
   filenames, LLM answers and planner queries are all attacker-influenced input: a chunk is
   whatever was in the uploaded file, and an answer is whatever a model generated from it.
   `textContent` cannot create an element, so markup in a chunk is shown as the characters
   it is.
   CSP is the **second** layer, not the first: verified that the CSP above stopped an
   `innerHTML` payload with `<img onerror=...>` from firing, while the same payload without
   CSP set `document.title`. Two independent layers, because the first one is a rule a
   future edit can break in one line.
   *On prompt injection:* a document that says "ignore your instructions" can already steer
   an answer today, through the API, with no browser involved. The playground does not add
   that risk and does not try to fix it. What it must not do is let such text *execute*, and
   it must make clear whose words are on screen: answers and chunks are shown in labelled,
   visually distinct blocks, never as if they were the page's own interface text.

7. **The API key is typed in, kept in `sessionStorage`, and never put in a URL.** A
   password-type field, stored per tab so a page reload does not lose it, gone when the tab
   closes. Not `localStorage` (a key that outlives the session on a shared machine), not a
   cookie (the API is bearer-authenticated; a cookie would be sent automatically and would
   invent a CSRF problem the service does not currently have), and never a query string
   (URLs end up in shell history, server logs and `Referer` headers).
   *Stated honestly:* any script running on this origin can read `sessionStorage`. That is
   why decisions 5 and 6 exist, and why decision 2 keeps the page off production hosts. A
   playground key should be a key for a test tenant, and the page says so.

8. **Retrieval provenance becomes an opt-in API field, and `SearchResult` stays a
   3-tuple.** `POST /search` and `POST /query` accept `"explain": true`; each hit then
   carries which arm found it, its rank in that arm, the fused score, and what reranking
   changed.
   *Why not widen the tuple:* `SearchResult` appears in the reranker `Protocol` (four
   signatures, including `LiteLLMReranker`'s index arithmetic and `FakeReranker`) and in the
   agent loop, which reads `row[0]` and `row[2]` positionally. Widening it rewrites all of
   them, for a debugging field. Instead `hybrid_search_with_provenance` returns
   `(rows, provenance_by_chunk_id)` and `hybrid_search` stays exactly as it is, as a thin
   wrapper - so every existing caller and every existing test is untouched. That the old
   tests still pass unchanged is the evidence that this is an addition, not a change.
   *Why opt-in:* the default response shape stays byte-identical for existing clients,
   including the PHP client that does not exist yet, and normal callers pay nothing.

9. **Python-only tests; the JavaScript is not executed in CI, and the plan says so.**
   Adding Playwright means a browser download in CI, a slower and flakier job, and a heavy
   dev dependency - against the AGENTS.md rule, for a page that is off in production.
   What **is** tested in `pytest`: the page is served when enabled and 404 when disabled;
   the CSP and content-type headers; that every file the HTML references exists; and the new
   `explain` fields end to end. Plus two guard tests that are worth more than they look:
   the shipped HTML contains **no `<script>` body and no `on*=` attribute** (so decision 5
   cannot silently rot), and `playground.js` contains **no `innerHTML`, `insertAdjacentHTML`,
   `document.write` or `eval`** (so decision 6 cannot silently rot). Those are real
   regressions, catchable in Python, without a browser.
   **The honest limit:** no test proves the page works. A broken button reaches `main`
   green. This phase therefore ships a short manual checklist in `docs/playground.md`, and
   the *Outcome* section below records what manual testing actually found.

10. **The demo is the playground, and the README gets at most two screenshots.** Screenshots
    go stale and are the most annoying kind of documentation debt, so: two at most, of the
    parts that will not change every week (an answer with its sources; the retrieval
    detail), regenerated only when they are wrong. The copy-pasteable `docs/demo.md` stays
    the authoritative walkthrough.

## Not in scope

- **Tenant and API key management in the browser** - permanently, not just this phase.
  A page that creates keys needs admin HTTP endpoints, and Phase 3 decision 4 deliberately
  did not build them: an endpoint that mints credentials needs its own authentication, and
  the CLI avoids that because shell access on the server *is* the authentication. Putting a
  dashboard in front of it would reintroduce exactly the problem that decision avoided. The
  playground asks for a key that `ragbridge-admin` printed; it never issues one.
- **A CORS middleware, or hosting the page anywhere but the app** - decision 4.
- **Conversation memory or a chat transcript that implies it.** Every API call is
  independent; there are no sessions. The page may keep past questions and answers visible
  on screen, but it must not send them back as context, because the API has nowhere to put
  them. A UI that looks like a chat but forgets everything is a lie about the product.
  Multi-turn conversations are a real feature with a real data model, and they are not this.
- **Per-source provenance for `POST /agent`.** The agent merges chunks across several
  searches, keeping each chunk's best score, so "its rank in the vector arm" has no single
  answer - a chunk can be found by keyword search in step 1 and vector search in step 2.
  `/agent` keeps what it already has (`steps`, with each query and its result count), which
  is the thing the maintainer asked to see. Per-step attribution (`found_in_step`) is a
  later idea, not this phase.
- **Streaming answers.** The API does not stream; adding SSE is its own feature, with its
  own decisions about the LiteLLM call, tracing and the cache.
- **Changing settings from the browser.** Chunk size, retrieval mode defaults, models and
  keys are environment variables read by `pydantic-settings` at startup. A page that wrote
  them would fight that design. Per-request overrides that the API *already* supports
  (`mode`, `top_k`, `max_steps`) are exposed, because they are request fields, not settings.
- **Rate limiting.** Still absent, still documented as a limitation in
  `docs/deployment.md`. The playground does not make it more urgent: it is off in
  production, and it cannot send requests faster than a person can click.
- **A design system, a component library, dark-mode theming, i18n, or mobile layouts.**
  One stylesheet, readable at a laptop window size.
- **Editing documents, re-chunking, or viewing a whole document's text.** List, upload,
  status, delete. The chunks shown are the ones retrieval returned.
- **Any new file type.** Whatever the API accepts today: `text/plain`, `text/markdown`,
  `application/pdf`.

## Data model changes

None. No migration in this phase.

## API changes

Additive and opt-in. No existing field changes, and no default response shape changes.

| Endpoint | Change |
|---|---|
| `POST /search` | New request field `explain: bool = False`. When true, each `SearchHit` gains `retrieval`, and the response gains `candidate_count`. |
| `POST /query` | Same `explain` field; each `Source` gains the same `retrieval` object. |
| `POST /agent` | None. Its sources reuse the shared `Source` model, so `retrieval` is present and `null` there - see *Not in scope*. |

`retrieval` reports, per hit: `vector_rank` (`int | null`), `keyword_rank` (`int | null`),
`fused_score`, and `rank_before_rerank`. With the default `NoOpReranker` the last one always
equals the hit's position, which is itself informative: it shows reranking is off.

**One correctness detail that must not be missed.** The answer cache key currently hashes
`question|mode|top_k` only. Adding `explain` without adding it to that digest would let a
cached plain answer be served to a request that asked for `explain`, and the reverse. It
goes into the digest, and a test proves it.

## New settings

| Setting | Default | Meaning |
|---|---|---|
| `ENABLE_PLAYGROUND` | `true` | Serve the playground page at `/playground`. `docker-compose.prod.yml` sets it to `false`, next to `ENABLE_DOCS`. |

`.env.example` documents it in the same style as `ENABLE_DOCS`.

## Production and development

- **Development (`docker compose up`):** the page is at `http://localhost:8000/playground`.
  Nothing else changes; no new service, no new port, no new container.
- **Running without Docker (`uvicorn --reload`):** identical, same URL. The files are
  static, so a browser reload is enough - no rebuild.
- **Production (`docker-compose.prod.yml` behind Caddy):** `ENABLE_PLAYGROUND=false`, so
  `/playground` is 404 and the static files are never mounted. Caddy needs **no change**: it
  reverse-proxies everything to the app already, and the page would be served through it
  untouched if an operator turned it on. Caddy's existing `X-Content-Type-Options: nosniff`
  applies to it as well; the CSP comes from the app either way (decision 5).
- **The image:** the three files ship inside the Python package (`src/ragbridge/static/`),
  so they are installed with it and need no `.dockerignore` change. They are a few
  kilobytes.

## Testing and CI

No new CI job and no new tooling. The existing `test` job covers everything; the
`deployment` job gains one assertion.

- `tests/test_playground.py`: served when enabled, 404 when disabled, correct
  `content-type`, CSP header present and containing `script-src 'self'`, every referenced
  asset resolves, and `/health` still works with the playground off.
- The two rot-guard tests from decision 9 (no inline script or `on*=` in the HTML; no
  `innerHTML`/`eval` family in the JS).
- `tests/test_search.py` / `tests/test_query.py`: `explain` off leaves the response
  unchanged; `explain` on reports the ranks; a chunk found by both arms has both ranks; a
  vector-only chunk has `keyword_rank: null`; `FakeReranker` (which reverses the order)
  makes `rank_before_rerank` differ, proving the field measures something real; tenant
  isolation is unchanged; and the answer cache distinguishes `explain` from plain.
- `tests/test_retrieval.py`: the provenance function agrees with `reciprocal_rank_fusion`
  on scores and ordering, and the existing `hybrid_search` tests pass **unchanged**.
- `scripts/smoke-prod.sh`: one more check - `/playground` is 404 in the production stack -
  taking it from 21 to 22. It costs nothing (no model involved) and it guards decision 2 on
  the real stack.

## What it costs to maintain

Stated up front, because this is the strongest argument against building it at all:

- Roughly 600-900 lines of HTML, CSS and JavaScript that **mypy and ruff never see**. This
  repo's whole quality story is `mypy --strict`, `ruff`, and tests for every feature. The
  playground is the first part of it with none of those guarantees.
- A standing temptation: every new API field invites a UI change, and every UI change
  invites a new API field. The *Not in scope* list above exists to be re-read, not just
  written once.
- Two screenshots that go stale.
- A second place where the API's shape is written down, which can disagree with the real
  API. The `explain` field is the first example: it is now in the code, the README, this
  plan and the page.

If this cost stops feeling worth it, the honest move is to delete the page and keep the
`explain` field, which is useful from curl too.

## Steps

1. Retrieval provenance in the API (`explain`) - code only, no UI
2. Serve the playground: the setting, the mount, the CSP, and an empty page
3. The playground itself: key entry, documents, and asking with the retrieval detail
4. Docs: README, `docs/playground.md`, ADR 0009, AGENTS.md roadmap, `.env.example`

Each step is one feature branch of several small commits, as in earlier phases.

**Why step 1 comes first, with no UI at all:** it is the only part with API consequences,
it is fully testable on its own, and it is useful from curl even if steps 2-4 are never
built. If this phase is abandoned after step 1, nothing is wasted.

## Step 1 detail — retrieval provenance

Branch: `feat/phase-6-retrieval-provenance`. No HTML, no static files, no new dependency.
Nothing changes for a caller that does not pass `explain`.

Proposed commit breakdown:

1. **`feat(retrieval): record which arm found each chunk`**
   - A frozen `ChunkProvenance` dataclass: `vector_rank: int | None`,
     `keyword_rank: int | None`, `fused_score: float`.
   - `hybrid_search_with_provenance(...) -> tuple[list[SearchResult], dict[UUID, ChunkProvenance]]`,
     and `hybrid_search` reduced to a one-line wrapper that drops the second value.
   - Single-arm modes (`vector`, `keyword`) fill the rank they have and leave the other
     `None`, with the arm's own score as `fused_score`.
   - Tests: a chunk returned by both arms has both ranks; a vector-only chunk has
     `keyword_rank is None`; ranks are 1-based; scores and ordering match
     `reciprocal_rank_fusion`; **and the existing `hybrid_search` tests are not edited** -
     that is the proof this is purely additive.
2. **`feat(api): add an opt-in explain field to POST /search`**
   - `explain: bool = False` on `SearchRequest`; a `RetrievalInfo` model; `SearchHit.retrieval`
     defaulting to `None`; `SearchResponse.candidate_count`.
   - `rank_before_rerank` is the hit's index in the candidate list before the reranker ran.
   - Tests: without `explain`, the response has no populated `retrieval` and the old
     assertions hold; with it, ranks appear; with `FakeReranker`, `rank_before_rerank`
     differs from the final position; tenant isolation unchanged.
3. **`feat(api): add explain to POST /query`**
   - The same field on `QueryRequest`, the same `retrieval` object on `Source` (so `/agent`
     serialises it as `null` - see *Not in scope*).
   - **`explain` joins the answer-cache digest**, next to `question|mode|top_k`.
   - Tests: as above, plus one that an answer cached without `explain` is *not* returned to
     a request that asks for it.
4. **`docs: document the explain field`**
   - A short README subsection under *Ask a question*, with a `curl` example, and a note
     that `rank_before_rerank` equalling the position means reranking is off.

Steps 2-4 get their own detailed breakdown when they are reached, as earlier phases did.

## Outcome

Written after the phase, so the plan above stays as it was decided and this records where
building it disagreed.

**Where reality differed from the plan**

- The plan said `/agent` would serialise `retrieval` as `null`. It does not: the endpoints
  answer with new `Explainable*` models, and `Source`, `QueryResponse` and `SearchResponse`
  are untouched, so `/agent` and both MCP tools are byte-for-byte what they were. A shared
  model had first made `search_documents` return two null fields and grow its output
  schema, and every existing test still passed. Two new tests now pin those MCP schemas.
- `response_model_exclude_none` was the first attempt at "unchanged unless asked". A test
  showed it also drops `keyword_rank: null` inside `retrieval`, which means "that arm did
  not find this chunk". It became `response_model_exclude_unset`, with the fields assigned
  only when `explain` is true.
- The cache-key hazard the plan predicted was real, and a second one was not in the plan: a
  cached answer written without `exclude_unset` came back with null fields a fresh answer
  does not have. Both directions are tested, and each protection was mutation-checked.
- The plan promised at most two screenshots. **One** shipped. With a clean four-file corpus
  `/agent` needed a single search, so its screenshot would have shown a multi-step tool
  doing one step; that is the Phase 5 lesson again (a tiny corpus makes multi-hop
  meaningless).
- The plan estimated 600-900 lines of untyped code. It is 883 (107 HTML, 262 CSS, 514 JS).
- Beyond the planned guards, tests also check that every id the script looks up exists,
  that the HTML has no inline styles (the policy blocks them silently), and that the policy
  contains no `unsafe-inline`, `unsafe-eval` or wildcard.
- The first hostile-document browser test proved nothing: the payload was in a different
  chunk than the words the query matched, so it was never retrieved. It was rewritten, and
  the page then deliberately broken with `innerHTML`: two elements were injected, the guard
  test failed, and only the CSP kept `document.title` intact.
- The new production smoke check was run against the real stack, and with the setting
  flipped to `true` it fails (`want 404, got 200`). That run also showed the page served
  through Caddy with no proxy change, as the plan claimed.
- Browser form validation refused a `top_k` of 99 before the server saw it. Correct
  behaviour, but it meant the server-error path needed a separate check.

**Results**

- The playground answers the question it was built for. Its first use turned up a real
  retrieval behaviour: the keyword arm **returns nothing for a plain-English question**,
  because `websearch_to_tsquery` ANDs every stem (`'mani' & 'busi' & 'day' & 'refund' &
  'take'`), so almost every source shows `keyword: not found`. A short query such as
  `refund business days` is found by both arms. **Not changed in this phase**; whether to
  relax it (an OR fallback, or dropping question words) is a retrieval decision with an
  evaluation to go with it.
- Prompt injection is visible and not fixed: the model obeyed an injected "reply with
  PWNED" in one run and not in another.
- Adding `explain` needed no change to the reranker `Protocol`, the agent loop, or a single
  existing test, which was the point of leaving `SearchResult` a 3-tuple.

**Still not done - stated, not hidden**

- The JavaScript is not executed in CI. A broken button can reach `main` green; the manual
  checklist in `docs/playground.md` is the substitute.
- Verified in Chrome only. Not verified: Firefox, Safari, Windows and Linux file-type
  behaviour (the page states the type from the extension in case a system reports none for
  Markdown; that did not reproduce on macOS Chrome), and narrow windows.
- Nothing about the playground has been run on a real server. It is off in production, and
  the smoke test proves that.
- The `explain` fields are unmeasured for cost. They reuse the arms' own result lists and
  add no query, but that was reasoned from the code, not benchmarked.
