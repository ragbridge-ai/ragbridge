# API guide

How to use ragbridge's HTTP API, with `curl`. Examples assume the API runs at
`http://localhost:8000` (see the [README](../README.md) to start it) and that you have a key
from `ragbridge-admin`. Every endpoint except `GET /health` and `GET /health/ready` needs
`Authorization: Bearer <key>`. With `ENABLE_DOCS=true` (the default), the generated
interactive reference is at `/docs`. To try all of this without `curl`, use the
[playground](playground.md).

## Upload a document

```bash
curl -H "Authorization: Bearer <key>" \
  -F "file=@notes.md;type=text/markdown" http://localhost:8000/documents
```

Text (`text/plain`), Markdown (`text/markdown`), and PDF (`application/pdf`) are
supported. Uploading the same content twice returns the existing document instead
of creating a duplicate (per tenant - two tenants uploading the same file each get
their own document). The response includes a `status`
(`pending` / `processing` / `ready` / `failed`): uploads at or under
`ASYNC_PROCESSING_THRESHOLD` come back `ready` immediately; larger ones come back
`pending` right away and are processed by the worker - poll
`GET /documents/{id}` until `status` is no longer `pending`/`processing`. A `failed`
document's `error` field explains why (a corrupt PDF, for example).

## Ask a question

```bash
curl -X POST http://localhost:8000/query \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"question": "What does this document say?", "top_k": 5}'
```

Returns `{"answer": "...", "sources": [...]}`. Each source reports the originating
document, its chunk position, a text snippet, a relevance score, and `context_only`.
**`sources` lists every chunk the model was given, one entry per chunk.** `top_k` (default 5,
at most 20) is the number of chunks *retrieved*: those come first, best first, with
`context_only: false`. They are followed by the chunks next to them that were given to the
model as surrounding text (see *What the answer model reads*): `context_only: true`, score
`0.0`, because retrieval did not score them. So `sources` can hold more than `top_k`
entries, bounded by `ANSWER_CONTEXT_MAX_CHARS`; set `ANSWER_CONTEXT_NEIGHBOURS=0` and it
holds exactly the retrieved chunks. Retrieval is
hybrid by default (vector + keyword search, merged with reciprocal rank fusion -
see [ADR 0003](adr/0003-hybrid-search-with-reciprocal-rank-fusion.md)); pass
`"mode": "vector"` or `"mode": "keyword"` in the request to use a single method
instead of `RETRIEVAL_MODE`'s default. Only documents belonging to the calling
tenant's key are ever searched.

### How the keyword search reads your text

Retrieval is hybrid: a vector search plus a keyword (full-text) search. The keyword search
first runs your query exactly as typed, so **every word must appear in one chunk**, which is
what makes an exact term such as an error code findable. Only if that finds nothing does it
retry as an **OR of the words**, ranked by how many of them each chunk holds; a query that
already matches is never loosened. A quoted phrase (`"refund policy"`) or a `-word` is
never relaxed, because that would drop what you asked for. Without the retry, one word that
is in no document, or a long question, would make the keyword search match nothing at all.

Before that, words that are in **most of your chunks** (more than `KEYWORD_MAX_TERM_FREQUENCY`,
default half) are left out of the keyword query: a product name or a word like "project" tells
chunks apart no better than "the" does, and it would let every generic chunk match and take
keyword credit ahead of the chunk that actually answers. This needs at least 20 chunks, is not
applied to a quoted phrase or a `-word`, and `1.0` turns it off.

When the fallback runs, chunks are ranked by how **rare** the words they contain are, so one
rare word (a specific term the question is really about) outweighs several common ones such as
"api" or "documentation". `KEYWORD_RARITY_WEIGHTING=false` restores PostgreSQL's plain ranking.

### What the answer model reads

`/query` does not hand the model the retrieved chunks as separate blocks in score order. A
chunk often ends with a heading whose bullets begin the next one, and read that way the
model attaches the last bullets of one company to the heading that follows them. So the
model is given each retrieved chunk **and its neighbours** (`ANSWER_CONTEXT_NEIGHBOURS`,
default 1) in **document order**, with chunks that were neighbours **joined into one
continuous excerpt** (the text two chunks share is written once), labelled
`[filename, chunks 2-4]` with the same `chunk_index` values the response reports, and a
note such as `[... chunks 5-6 are not shown ...]` where text is missing between excerpts.
Neighbours are added only up to `ANSWER_CONTEXT_MAX_CHARS`, so a local model's small
context window is not overflowed. The prompt also says that a bullet belongs to the
heading above it. Every chunk the model received is listed in the response's `sources`:
the retrieved ones first, in score order, then the neighbours marked `context_only`. None
of this needs a re-upload: it happens at query time.

The answer is sampled at `CHAT_TEMPERATURE` (default `0`), so the same question over the same
context gives the same answer. Without it Ollama samples at 0.8, and a 7B model read one bullet
under a different company's heading from run to run. One limit stays, measured in
[the evaluation notes](evaluation.md#the-answer-model-repeated-runs): if the chunk holding a
bullet's heading is *not* in the context (neighbours turned off, or dropped by
`ANSWER_CONTEXT_MAX_CHARS`), the model attaches the bullet to the heading below it every time,
whatever the temperature. Check `sources` for `context_only` chunks to see what it received.

## See why a chunk was found

Add `"explain": true` to a `/query` or `/search` request to get, for every returned
chunk, how retrieval found it:

```bash
curl -X POST http://localhost:8000/search \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"query": "refund policy", "top_k": 2, "explain": true}'
```

Each result gains a `retrieval` object, and the response gains `candidate_count` (how
many chunks were found before reranking narrowed them to `top_k`). The scores below are
illustrative:

```json
{
  "results": [
    {
      "filename": "policy.md",
      "content": "...",
      "score": 0.0323,
      "retrieval": {
        "vector_rank": 1,
        "keyword_rank": 3,
        "fused_score": 0.0323,
        "rank_before_rerank": 1
      }
    }
  ],
  "candidate_count": 12
}
```

- `vector_rank` and `keyword_rank` are the chunk's 1-based position in each retrieval arm.
  **`null` means that arm did not find the chunk at all** - for example, a chunk with
  none of the question's words has no `keyword_rank`.
- `fused_score` is the score retrieval gave the chunk before reranking: the merged score
  in `hybrid` mode, that arm's own score in `vector` or `keyword` mode. With reranking
  off, it equals `score`.
- `rank_before_rerank` is the chunk's position before the reranker ran. When it always
  equals the chunk's position in the results, reranking is off (the default).

Without `explain` the response is exactly what it was before. `/agent` does not offer it: the
agent merges chunks found by several searches, so a single arm's rank has no clear meaning
there. The MCP tool `search_documents` does (see [MCP](#mcp)); `ask` does not.

## Search without generating an answer

```bash
curl -X POST http://localhost:8000/search \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"query": "refund policy", "top_k": 5}'
```

Returns `{"results": [...]}`: the same retrieval and reranking as `/query`, stopping
before generation. Each result is a **whole chunk** (`content`, not a snippet) with its
document, chunk position, and score - for callers that do their own reasoning over the
text.

## Ask a multi-step question

```bash
curl -X POST http://localhost:8000/agent \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{"question": "How does our refund policy differ from our cancellation policy?"}'
```

Returns `{"answer": "...", "sources": [...], "steps": [...], "step_count": N}`. The agent
searches for the question, then a planner model decides whether another search with a
different query would help, up to `AGENT_MAX_STEPS` searches. `steps` lists each query
and how many chunks it found, so a wrong answer can be traced to what was searched.
Chunks found by several searches appear once. A request may pass `"max_steps"` to use
fewer searches, never more than the server allows.

The planner must reply with a small JSON object. **If it cannot** - a reply that is not
valid JSON, names an unknown action, or asks to search for nothing - the agent answers
with what it has found so far, so `/agent` degrades to `/query` behaviour instead of
failing ([ADR 0006](adr/0006-hand-rolled-agent-loop-instead-of-langgraph.md)).

**Measured, and worth knowing before you rely on it:** how well `/agent` works depends almost
entirely on the chat model. On a test built for it (answers reachable only through a chain
of documents), the default `llama3.2` was **not** meaningfully better than `/query` - it
usually decided one search was enough. `qwen2.5:7b` answered **29 of 30** two-step questions
correctly, twice. Three-step questions stay unreliable even then (13-33% correct). On a 16 GB
laptop `qwen2.5:7b` also scored 43/43 on a 43-question answering test in every run, at about
half `llama3.2`'s speed. Set it with `CHAT_MODEL=ollama/qwen2.5:7b`. Full method, numbers and
caveats: [docs/evaluation.md](evaluation.md).
Each call is independent; there are no sessions or follow-up questions.

## List, fetch, and delete documents

```bash
curl -H "Authorization: Bearer <key>" http://localhost:8000/documents
curl -H "Authorization: Bearer <key>" http://localhost:8000/documents/<id>
curl -H "Authorization: Bearer <key>" -X DELETE http://localhost:8000/documents/<id>
```

## Keep documents in sync with your records (external ids)

Uploading files is enough for a folder of PDFs. If your application already has records
(articles, products, help pages) with their own ids, identify each document by **your**
id instead. You then never store ragbridge's ids, an edit replaces the old text instead of
adding a second document, and every call is safe to retry.

```bash
curl -X PUT http://localhost:8000/documents/external/article:42 \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"title": "Refund policy",
       "content": "Refunds are possible within 14 days of purchase.",
       "metadata": {"locale": "en"},
       "source_updated_at": "2026-09-21T10:00:00Z"}'
```

```json
{
  "result": "created",
  "document": {
    "id": "9d1d1c75-049e-4609-802a-8e3fa21c4a4e",
    "external_id": "article:42",
    "filename": "Refund policy",
    "content_type": "text/plain",
    "status": "ready",
    "error": null,
    "metadata": {"locale": "en"},
    "source_updated_at": "2026-09-21T10:00:00Z",
    "created_at": "2026-09-21T14:38:16.201847Z",
    "updated_at": "2026-09-21T14:38:16.201847Z"
  }
}
```

| Call | What it does |
|---|---|
| `PUT /documents/external/{external_id}` | Create the document, or replace it. Send the record's **current** state every time. |
| `GET /documents/external/{external_id}` | The document, or 404. Poll it after a `202`. |
| `DELETE /documents/external/{external_id}` | Delete it. **204 also when it does not exist**, so a retried delete is not an error. |

The body of `PUT` has `title` (1-500 characters; shown as the document's name in
`sources`), `content` (text with at least one character that is not whitespace, at most
`MAX_UPLOAD_SIZE` bytes as UTF-8), and optionally `metadata` (a JSON object, at most 16 KB,
returned as sent; it is **not** searched yet) and `source_updated_at` (with a time zone). A
document made this way is listed by `GET /documents` and deletable by its UUID like any other,
and is searched exactly like an upload.

### What `result` means

| `result` | Status | What happened |
|---|---|---|
| `created` | 201 (202 if queued) | A new document. |
| `replaced` | 200 (202 if queued) | The text changed: the chunks were replaced and re-embedded. |
| `updated` | 200 | Only the title or `metadata` changed. **Nothing was re-chunked or re-embedded.** |
| `unchanged` | 200 | The same record again. Nothing was written. |
| `stale` | 200 | Ignored: `source_updated_at` is older than the stored one (below). |

Whether the text changed is decided by its SHA-256, so a job that re-sends every record
after every save is cheap: the records that did not change cost one lookup and no embedding.
Changing a title also drops the tenant's cached answers, because they name their sources;
changing only `metadata` does not.

### Ordering: `source_updated_at`

Queues deliver out of order. If a `PUT` carries a `source_updated_at` **older** than the one
stored, ragbridge ignores it and answers `200` with `"result": "stale"` and the document as
it stands. That is not an error: the newer state is already there. Equal times are applied (it
is nearly always a retry), and a `PUT` without `source_updated_at` is always applied and keeps
the stored time. Send the time your application's record was last changed, **not** the time
you make the call.

### The id

`external_id` is 1-255 characters from `A-Z a-z 0-9 . _ : @ -` and starts with a letter or
digit (`^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$`); it is case-sensitive, and unique per tenant, so
two applications can both have an `article:42`. Anything else is a `422` that names the
pattern. There is no `/`: an encoded slash is decoded before ragbridge sees the path, so its
meaning would depend on every proxy in between; use `article:42` or `wp_posts.42`. None of the
allowed characters needs URL encoding, but encoding is harmless (`rawurlencode('article:42')` is
`article%3A42`, which arrives as `article:42`).

### Large content

Content over `ASYNC_PROCESSING_THRESHOLD` bytes is saved as `pending` and embedded by the
worker, as for uploads: the response is `202`, and you poll `GET /documents/external/{id}`
until `status` is `ready` (or `failed`, with `error`). When it replaces an older version, **the
old version stays searchable until the new one is ready**, and stays if the new one fails; send
the same record again to retry a `failed` one. If you send new text while an older version is
still queued, only the newest is embedded. If ragbridge cannot reach its queue you get `503`
and the document is marked `failed`, so sending the record again works.

### Concurrent calls

Two `PUT`s for the same id at the same moment never create two documents: the database
allows one row per tenant and id, the second call waits for the first and then decides against
what it saved. Calls for different ids do not wait for each other. A call that keeps losing a
race with a delete answers `409`; send it again.

### Not supported yet

- **Searching or filtering by `metadata`.** It is stored so you do not have to re-send everything
  once filtering exists.
- **Remembering deletes.** After `DELETE`, a delayed `PUT` with an older `source_updated_at`
  creates the document again.
- **Many records in one request**, and **files** (a PDF for a record: use `POST /documents`).

Why it is built this way: [ADR 0010](adr/0010-external-document-ids.md) and the
[plan](plans/external-ids.md).

## Multi-tenancy and API keys

Every request except `GET /health` and `GET /health/ready` needs
`Authorization: Bearer <key>`; a key
belongs to exactly one tenant, and a tenant's documents, chunks, and answers are
invisible to every other tenant. Manage tenants and keys with `ragbridge-admin`:

```bash
uv run ragbridge-admin create-tenant --name acme        # new tenant + first key
uv run ragbridge-admin list-tenants                     # list all tenants
uv run ragbridge-admin create-key --tenant-id <id> --name second-key
uv run ragbridge-admin revoke-key --prefix rb_abcdefgh  # prefix from list output
```

A key is printed once, at creation time, and stored only as a SHA-256 hash - there
is no way to recover a lost key, only to revoke it and create a new one.

## MCP

ragbridge is also an [MCP](https://modelcontextprotocol.io) server, so a client such as
Claude Desktop can search a tenant's documents. It exposes three tools -
`search_documents` (whole chunks, for the client's own model to reason over), `ask` (a
finished answer with sources: `top_k` is 5, and sources marked `context_only` were given to
the model as surrounding text but not scored by retrieval), and `list_documents` - and deliberately not the agent: an
MCP client is already an agent and can call `search_documents` repeatedly itself.

`search_documents` takes an optional `explain` (boolean, default `false`). With `explain: true` the
result also carries, for every passage, the same `retrieval` object as `POST /search` (the rank in the
vector and the keyword search, `null` meaning that search did not find it; the fused score; the rank
before reranking) and `candidate_count`. Without it the request and the result are exactly what they
were, and the tool's declared output schema does not change either way. The tool's *input* schema does
gain `explain`, so **a client that cached the tool list, such as Claude Desktop, must be restarted**
before the model can use the flag.

**Over HTTP**, the server is mounted at `/mcp` (streamable HTTP) and takes the same
`Authorization: Bearer <key>` as every other endpoint. A request with no bearer token
gets `401`.

**Over stdio**, for desktop clients, run `ragbridge-mcp`. It is a thin proxy to a
*running* ragbridge, so it needs no database or Redis of its own:

```bash
RAGBRIDGE_API_KEY=<key> RAGBRIDGE_BASE_URL=http://localhost:8000 uv run ragbridge-mcp
```

`RAGBRIDGE_API_KEY` is required; `RAGBRIDGE_BASE_URL` defaults to
`http://localhost:8000`. In Claude Desktop's config:

```json
{"mcpServers": {"ragbridge": {
  "command": "uv",
  "args": ["--directory", "/path/to/ragbridge", "run", "ragbridge-mcp"],
  "env": {"RAGBRIDGE_API_KEY": "<key>"}
}}}
```

Both transports send every tool call through the REST API with the caller's own key, so
tenant isolation applies exactly as it does everywhere else - see
[ADR 0007](adr/0007-mcp-server-on-the-mcp-sdk-2x.md).
