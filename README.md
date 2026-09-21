# ragbridge

Open-source, ready-to-use RAG (Retrieval-Augmented Generation) service. It lets any
existing application (for example a Laravel, Symfony, or WordPress app) add chat and
smart search over its own data, through an HTTP API, without moving the application to Python.

![The playground answering a question, with each source's retrieval detail](docs/img/playground-query.png)

*The [playground](docs/playground.md): an answer, its sources, and which search found each one.*

## What you get

- Upload text, Markdown and PDF documents; large files are processed by a background worker.
- Keep documents in sync with your own records by **your own ids**: one idempotent `PUT` per record.
- Hybrid retrieval (vector + keyword) with optional reranking, and answers **with sources**.
- A bounded multi-step agent endpoint, and an [MCP](https://modelcontextprotocol.io) server.
- API keys and multi-tenancy: a tenant never sees another tenant's data.
- Caching, Langfuse tracing, and a production Compose file with a TLS proxy.
- A browser [playground](docs/playground.md) to try it on your own files and see what retrieval did.

## Quick start

You need Docker and [Ollama](https://ollama.com) (or a hosted model provider, see
[configuration](docs/configuration.md)). Python 3.12 and [uv](https://docs.astral.sh/uv/) are
only needed to run the admin commands or to work on the code.

```bash
cp .env.example .env
ollama pull nomic-embed-text
ollama pull llama3.2
docker compose up --build
uv run ragbridge-admin create-tenant --name acme     # prints an API key once: save it
```

Then open the playground at <http://localhost:8000/playground/>, or use `curl`:

```bash
curl -H "Authorization: Bearer <key>" \
  -F "file=@notes.md;type=text/markdown" http://localhost:8000/documents

curl -X POST http://localhost:8000/query \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"question": "What does this document say?"}'
```

## The API

| Endpoint | What it does |
|---|---|
| `POST /documents`, `GET /documents`, `DELETE /documents/{id}` | Upload, list and delete documents |
| `PUT`, `GET`, `DELETE /documents/external/{your_id}` | Keep a document in sync with a record of your application |
| `POST /query` | An answer, with its sources |
| `POST /search` | The best-matching chunks, without an answer |
| `POST /agent` | Several searches, then an answer, listing the searches it ran |
| `/mcp` | The same search and answers for MCP clients |

`/query`'s `sources` lists every chunk the model was given: the `top_k` retrieved ones first,
then neighbouring chunks marked `"context_only": true` (given as surrounding text, not
scored by retrieval, score `0.0`).

Add `"explain": true` to `/query` or `/search` to see which search found each chunk.
Details and examples: [API guide](docs/api.md).

## Keeping it in sync with your application

Give each record's document **your** id, and send the record whenever it is saved. The call
is idempotent, so a retry or a full re-sync is safe, and text that did not change is never
embedded again. In a Laravel observer, for example:

```php
Http::withToken(config('services.ragbridge.key'))
    ->put(config('services.ragbridge.url').'/documents/external/'.rawurlencode('article:'.$article->id), [
        'title' => $article->title,
        'content' => strip_tags($article->body),
        'metadata' => ['locale' => $article->locale],
        'source_updated_at' => $article->updated_at->toIso8601String(),
    ])->throw();

// and in the deleted() hook:
Http::withToken(config('services.ragbridge.key'))
    ->delete(config('services.ragbridge.url').'/documents/external/'.rawurlencode('article:'.$article->id))
    ->throw();
```

Details, the response, ordering with `source_updated_at`, and the limits:
[API guide](docs/api.md#keep-documents-in-sync-with-your-records-external-ids).

## Documentation

- [API guide](docs/api.md) · [Playground](docs/playground.md) · [Configuration](docs/configuration.md)
- [Deployment](docs/deployment.md): a single Linux server behind a TLS proxy
- [Demo](docs/demo.md) (a real run, output included) · [Evaluation](docs/evaluation.md) ·
  [Development](docs/development.md) · [Decisions (ADRs)](docs/adr/) · [Roadmap](AGENTS.md)

## Status

**v1.3.0.** Measured, with the limits written down: `/agent` depends heavily on the chat model
(`qwen2.5:7b` is much better than the default `llama3.2`, see [evaluation](docs/evaluation.md)),
and the playground's JavaScript is checked by hand, not by CI. **Not yet run on a real server or
with a real domain**: the [deployment guide](docs/deployment.md) says exactly what was verified.

## License

MIT, see [LICENSE](LICENSE).
