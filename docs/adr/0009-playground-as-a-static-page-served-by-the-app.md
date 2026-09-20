# 9. A playground served by the app as plain HTML, CSS and JavaScript

Date: 2026-09-19

## Status

Accepted. Reverses the "no web UI" line in the Phase 5 plan's *Not in scope*, for one
narrow, non-product page. See [docs/plans/phase-6-ui.md](../plans/phase-6-ui.md).

## Context

The product is an HTTP API for other applications, and that does not change. What was
missing was a way to *look at* it: trying the service on your own files needed `curl`
and a shell, and there was no way to see which chunks retrieval found for a question, how
they scored, or what `/agent` searched for. The evaluation scripts measure aggregates over
a fixed corpus; they cannot answer "why was the answer about *my* file wrong?".

"No UI, just better documentation" was considered and rejected: no amount of prose shows
the chunks retrieved from your own PDF.

## Decision

1. **A playground at `/playground`, not a UI product.** One page: paste an API key,
   upload and delete documents, ask through `/query`, `/search` or `/agent`, and see the
   answer, the sources, their scores, and which retrieval arm found each one.
2. **Plain HTML, CSS and JavaScript, served by the app with `StaticFiles`.** No build
   step, no framework, no npm, no new Python dependency. A page with a handful of
   interactions does not need a bundler, and AGENTS.md says to add a tool only when the
   phase needs it.
3. **Same origin, so no CORS middleware.** Verified in a browser: a same-origin `fetch`
   with an `Authorization` header works with no CORS headers at all; the same code from
   another origin is blocked. Hosting the page elsewhere would have forced a CORS policy
   into a service that has none, and a CORS policy is a permanent security decision.
4. **A strict Content-Security-Policy, set by the app** (everything from this origin, no
   inline script or style). Set by the app rather than Caddy because Caddy only exists in
   production and this page mostly runs in development.
5. **All server and document text is rendered with `textContent`.** A chunk is whatever was
   in the uploaded file and an answer is whatever a model wrote from it; neither may create
   an element. The CSP is a second, independent layer.
6. **The key lives in `sessionStorage`**: per tab, gone when the tab closes; not
   `localStorage`, not a cookie (which would be sent automatically and invent a CSRF
   problem the API does not have), and never in a URL.
7. **Off in production by an explicit setting**, `ENABLE_PLAYGROUND` (default `true`, set to
   `false` by `docker-compose.prod.yml`), the same shape as `ENABLE_DOCS`. A smoke-test
   check asserts `/playground` is 404 on the real production stack.
8. **Retrieval detail is an opt-in API field, `explain`**, on `/search` and `/query`. It is
   useful from `curl` too, so it is not tied to the page. `SearchResult` stays a plain
   3-tuple; a separate `ChunkProvenance` travels beside it.
9. **No admin features, permanently.** The page never creates tenants or keys: an endpoint
   that mints credentials needs its own authentication, which is why Phase 3 decision 4
   left that to the CLI.

## What building this found

- **A shared response model would have changed the MCP tools.** Adding `retrieval` and
  `candidate_count` to `SearchResponse` made the `search_documents` tool return null
  fields and grow its output schema. Every existing test still passed. The endpoints now
  answer with separate `Explainable*` models and two tests pin the MCP schemas.
- **`exclude_none` was the wrong tool.** A `keyword_rank` of `null` means "that arm did not
  find this chunk", which is information; `response_model_exclude_unset` omits only the
  fields the endpoint never set.
- **The answer cache key had to include `explain`**, or a plain cached answer would have
  been served to a request that asked for the detail, and the reverse.
- **The first hostile-document test proved nothing.** The payload landed in a different
  chunk than the words the query matched, so it was never shown. It was rewritten and then
  mutation-checked: with `el()` switched to `innerHTML`, two elements were injected and
  only the CSP kept `document.title` intact.
- **The keyword arm returns nothing for a plain-English question.** `websearch_to_tsquery`
  joins every stem with AND (`'mani' & 'busi' & 'day' & 'refund' & 'take'`), and no chunk
  holds them all; the short query `refund business days` matched. This is existing
  retrieval behaviour that the playground made visible. It was not changed here; it was
  fixed afterwards (2026-09-20): first a question of four or more words became an OR of its
  words, then (the same day, after that rule missed queries containing `or` and short queries
  with an absent word) the query is run as typed and retried as an OR only when it matches
  nothing, see `or_fallback_query` in `src/ragbridge/retrieval.py`.

## Consequences

- About 880 lines of HTML, CSS and JavaScript (107 + 262 + 514) that `mypy` and `ruff` never see. The
  JavaScript is **not executed in CI**. What is checked as plain text: the page is served
  with the right headers, has no inline script, event handler or style, every id the script
  looks up exists, and the script never builds markup from text. Behaviour was checked by
  hand in headless Chrome (Chrome only), and `docs/playground.md` has the checklist.
- A second place where the API's shape is written down, which can drift from the real one.
- If the page ever needs tabs, routing and shared state, the answer is to stop growing it,
  not to add a framework. Deleting the page and keeping `explain` is a valid later choice.
