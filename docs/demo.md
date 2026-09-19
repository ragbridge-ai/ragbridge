# Demo

A walkthrough of ragbridge from an empty install to an answer. **Every output block
below was captured from a real run on 2026-09-19** - nothing was written by hand except
the surrounding text, and the API key is masked. Models: `ollama/llama3.2` for answers
and planning, `ollama/nomic-embed-text` for embeddings, both through a local Ollama.

The stack was the production Compose file (see [deployment.md](deployment.md)), reached
over plain HTTP on a local port. The commands below use two variables, so they work
against any install:

```bash
export URL=http://localhost:8000     # where ragbridge is reachable
export KEY=rb_<your-key>             # from step 1
```

Before you start: a running ragbridge (`docker compose up --build` from the
[README](../README.md)) and Ollama with both models pulled. Scores and document ids
vary between runs; document ids are shown as `<uuid>`.

### 1. Create a tenant and an API key

Run once, on the server. The key is shown once and stored only as a hash.

```bash
docker compose exec app ragbridge-admin create-tenant --name demo
```

Output:

```text
Tenant 3f78c67c-9cc5-464e-93db-d657d9cd36e7 (demo) created.
API key: rb_<your-key>
This key is shown once. Only its hash is stored - save it now.
```

### 2. Upload documents

Six short markdown files from `evaluation/corpus/` (a made-up company's policies).

```bash
for f in evaluation/corpus/*.md; do
  curl -s -o /dev/null -w "%{http_code}  $f\n" \
    -H "Authorization: Bearer $KEY" -F "file=@$f;type=text/markdown" $URL/documents
done
```

Output:

```text
201  api-error-codes.md     status=ready
201  billing-faq.md         status=ready
201  hr-policy.md           status=ready
201  onboarding-guide.md    status=ready
201  refund-policy.md       status=ready
201  security-policy.md     status=ready
```

### 3. Search: the passages, without an answer

Whole chunks with their source and score - for callers that reason over the text themselves.

```bash
curl -s -X POST $URL/search -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"query": "how long do refunds take", "top_k": 2}'
```

Output:

```json
{
  "results": [
    {
      "document_id": "<uuid>",
      "filename": "refund-policy.md",
      "chunk_index": 1,
      "content": "**Refund window.** Customers may request a full refund within 14 days of purchase, no questions asked. After 14 days, refunds are granted only for documented service outages.",
      "score": 0.01639344262295082
    },
    {
      "document_id": "<uuid>",
      "filename": "refund-policy.md",
      "chunk_index": 3,
      "content": "**How to request a refund.** To request a refund, email billing@acmecloud.example with your invoice number. Refund requests are processed within 5 business days.",
      "score": 0.016129032258064516
    }
  ]
}
```

### 4. Ask a question

Retrieval, then an answer written only from what was retrieved, with its sources.

```bash
curl -s -X POST $URL/query -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"question": "Can I still get a refund three weeks after buying?", "top_k": 3}'
```

Output:

```json
{
  "answer": "No, you can request a full refund within 14 days of purchase, no questions asked. If you wait three weeks, you would need to wait for a documented service outage to be eligible for a refund.",
  "sources": [
    {
      "document_id": "<uuid>",
      "filename": "refund-policy.md",
      "chunk_index": 1,
      "snippet": "**Refund window.** Customers may request a full refund within 14 days of purchase, no questions asked. After 14 days, refunds are granted only for documented service outages.",
      "score": 0.01639344262295082
    },
    {
      "document_id": "<uuid>",
      "filename": "refund-policy.md",
      "chunk_index": 3,
      "snippet": "**How to request a refund.** To request a refund, email billing@acmecloud.example with your invoice number. Refund requests are processed within 5 business days.",
      "score": 0.016129032258064516
    },
    {
      "document_id": "<uuid>",
      "filename": "refund-policy.md",
      "chunk_index": 4,
      "snippet": "**Partial refunds.** If a customer downgrades mid-cycle, Acme Cloud issues a prorated credit toward the next invoice rather than a cash refund.",
      "score": 0.015873015873015872
    }
  ]
}
```

### 5. Ask a multi-part question with the agent

`steps` records each search the agent ran. Read it before trusting the answer: see the note below.

```bash
curl -s -X POST $URL/agent -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"question": "What happens to my data after I cancel, and can I still request a refund by then?"}'
```

Output:

```json
{
  "answer": "According to the context, after you cancel, your customer data is retained for 90 days and then permanently deleted. You can request an earlier deletion by contacting support, but you can still request a refund within 14 days of purchase, no questions asked.",
  "sources": [
    {
      "document_id": "<uuid>",
      "filename": "security-policy.md",
      "chunk_index": 3,
      "snippet": "**Data retention.** Customer data is retained for 90 days after account cancellation, after which it is permanently deleted. Customers can request earlier deletion by contacting support.",
      "score": 0.01639344262295082
    },
    {
      "document_id": "<uuid>",
      "filename": "refund-policy.md",
      "chunk_index": 3,
      "snippet": "**How to request a refund.** To request a refund, email billing@acmecloud.example with your invoice number. Refund requests are processed within 5 business days.",
      "score": 0.016129032258064516
    },
    {
      "document_id": "<uuid>",
      "filename": "refund-policy.md",
      "chunk_index": 1,
      "snippet": "**Refund window.** Customers may request a full refund within 14 days of purchase, no questions asked. After 14 days, refunds are granted only for documented service outages.",
      "score": 0.015873015873015872
    },
    {
      "document_id": "<uuid>",
      "filename": "refund-policy.md",
      "chunk_index": 5,
      "snippet": "**Chargebacks.** Customers who file a chargeback instead of contacting support first will have their account suspended until the dispute is resolved.",
      "score": 0.015625
    },
    {
      "document_id": "<uuid>",
      "filename": "refund-policy.md",
      "chunk_index": 2,
      "snippet": "**Non-refundable items.** Custom onboarding packages and one-time setup fees are non-refundable once the onboarding call has taken place.",
      "score": 0.015384615384615385
    }
  ],
  "steps": [
    {
      "query": "What happens to my data after I cancel, and can I still request a refund by then?",
      "results": 5
    }
  ],
  "step_count": 1
}
```


**Read that answer critically.** The agent took one step (`step_count: 1`) - its
planner judged the first search sufficient - and the answer is only half right. It
found both facts (90-day retention, 14-day refund window) but did not reconcile them:
asked whether a refund is still possible "by then" (up to 90 days), the correct answer
is *no, except for a documented outage*, and it says "you can still request a refund
within 14 days". A stronger planner or answering model may do better; with the default
`llama3.2`, [docs/evaluation.md](evaluation.md) measured `/agent` as not meaningfully
better than `/query`; with `qwen2.5:7b` it handled two-step questions well. Prefer `/query`
unless you have tested `/agent` on your questions and model.

### 6. Use it from an MCP client

The stdio server is a thin proxy to the running service. This is what a desktop client such as Claude Desktop does on your behalf.

```bash
RAGBRIDGE_API_KEY=$KEY RAGBRIDGE_BASE_URL=$URL uv run ragbridge-mcp   # then a client calls:
#   list_tools, search_documents(query="password length requirement", top_k=1), list_documents()
```

Output:

```text
tools: search_documents, ask, list_documents

search_documents ->
{
  "results": [
    {
      "document_id": "<uuid>",
      "filename": "security-policy.md",
      "chunk_index": 1,
      "content": "**Passwords.** Acme Cloud requires passwords to be at least 12 characters long and include a mix of letters, numbers, and symbols. Passwords must be changed every 180 days.",
      "score": 0.01639344262295082
    }
  ]
}

list_documents ->
[
  "security-policy.md",
  "refund-policy.md",
  "onboarding-guide.md",
  "hr-policy.md",
  "billing-faq.md",
  "api-error-codes.md"
]
```

## What this shows, and what it does not

- Retrieval found the right passages, and the answers to a plain question are grounded
  in them. The corpus is six short documents, so retrieval here is easy - see
  [evaluation.md](evaluation.md) for measurements on harder questions.
- It is a demo of the *interface*, not a benchmark. One run per question, one small
  model, and answer quality varies from run to run.
