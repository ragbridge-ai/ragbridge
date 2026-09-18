# 5. Cost tracking and tracing with Langfuse

Date: 2026-09-18

## Status

Accepted, with a documented limitation (see Consequences).

## Context

Phase 3 needs cost tracking and tracing (`AGENTS.md` roadmap: "tracing and cost
tracking with Langfuse"). LiteLLM already computes token counts and per-model
prices for every provider it supports - Ollama, Anthropic, OpenAI, Voyage - so
building a separate pricing table in this project would duplicate work LiteLLM
already does and would silently go stale as providers change their prices.
LiteLLM exposes this through a callback mechanism (`litellm.success_callback`,
`litellm.failure_callback`) rather than a return value, so adopting it is a
registration, not a change to every call site that already goes through
`LiteLLMEmbedder`/`LiteLLMChatter`.

Everything below was verified against the actually-installed versions
(`litellm==1.101.0`, `langfuse==4.15.4`) and a real local Ollama instance before
being written into `ragbridge.tracing` or this ADR - `pyproject.toml` pins only a
lower bound on `litellm` and `langfuse` (matching every other dependency in this
project), and both have moved fast enough that guessing from documentation would
not have produced working code, the same lesson `docs/adr/0004-...` recorded for
RAGAS.

### Issue 1: `litellm.success_callback = ["langfuse"]` - the callback name most of LiteLLM's own documentation shows - is broken against the current `langfuse` SDK

Registering it and making one real call (verified against `ollama/llama3.2`) raises
`AttributeError: module 'langfuse' has no attribute 'version'` the moment the call
completes. `litellm`'s `langfuse.py` integration reads `langfuse.version.__version__`
to detect which SDK features are available; that module path existed on the
`langfuse` v2 SDK and was removed when `langfuse` v3 rewrote itself around
OpenTelemetry (the currently-installable `langfuse` is v4). LiteLLM ships a second,
OpenTelemetry-native integration for this SDK generation:
`litellm.success_callback = ["langfuse_otel"]`. Confirmed working end to end against
the same real Ollama call, span open to close, with a real (deliberately invalid)
Langfuse key - the only failure was the expected `401 Unauthorized` on export, not a
code-level incompatibility. `ragbridge.tracing.configure` registers `"langfuse_otel"`,
never `"langfuse"`.

### Issue 2: LiteLLM's Langfuse integrations read credentials from `os.environ`, not from this project's `Settings`

Both `"langfuse"` and `"langfuse_otel"` read `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`, and `LANGFUSE_HOST` directly from the process environment.
`pydantic-settings` reads a `.env` file into its own merged configuration without
exporting those values back into `os.environ` - this project's normal configuration
path (every setting so far has been read this way) would otherwise be invisible to
LiteLLM's integration even though `Settings().langfuse_public_key` sees it correctly.
`ragbridge.tracing.configure` bridges the three settings into `os.environ` itself,
once, before registering the callback.

## Decision

`ragbridge.tracing.configure(settings)` registers `litellm.success_callback` and
`litellm.failure_callback = ["langfuse_otel"]` when both `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` are set, bridging all three settings into `os.environ` first.
With either key unset, it does nothing - no callback, no import of `langfuse`'s
client beyond what `litellm` itself imports lazily. `create_app()` calls it once,
at app creation.

`ragbridge.tracing.span(settings, name, **input_fields)` is a context manager used
by `POST /query` to wrap embedding, retrieval, reranking, and generation in one
span, so LiteLLM's own per-call spans have a parent to nest inside - retrieval is
not an LLM call and would otherwise never appear in a trace at all. It yields a
`_NullSpan` (a real, harmless implementation of the same `.update()` interface, not
an `if` at the call site - the same shape as `NoOpReranker` from Phase 2) when
tracing isn't configured.

## Consequences

- Tracing is genuinely absent, not merely inactive, until both keys are set:
  `configure()` and `span()` are no-ops with the default empty settings, verified
  by running the full test suite with them unset (unchanged) and by unit tests that
  assert `litellm.success_callback` stays `[]`.
- **Known limitation, found while verifying the real end-to-end path (a real
  FastAPI app, real Ollama, tracing enabled with a real-shaped but invalid
  Langfuse key) - not caught by the test suite, since CI never configures
  tracing:** wrapping **two or more** LiteLLM calls inside one
  `tracing.span(...)` block produces repeated `"Setting attribute on ended span"`
  / `"Tried calling set_status on an ended span"` warnings from the OpenTelemetry
  SDK. Reproduced with the minimum case - two bare `litellm.acompletion` calls
  inside one manually-opened `langfuse_otel` span, no FastAPI involved - so this is
  a `litellm==1.101.0` internal issue in how its OTel Langfuse integration handles
  a second nested call under a span it did not create itself, not a defect in
  `ragbridge.tracing`. `POST /query` normally makes exactly two calls (an embedding,
  then a chat completion) under one span, so this affects it in practice: the
  request still succeeds and returns the correct answer (verified), but the
  second call's span may be missing some attributes in Langfuse, and the log
  fills with warnings. Two ways to fix this if it does not resolve itself on a
  `litellm` upgrade (re-verify first):
  - Correlate calls by a shared `trace_id` in each call's `metadata` instead of
    nesting them under a live span (`litellm`'s own supported mechanism, seen in
    `litellm/integrations/langfuse/langfuse_otel.py`). This needs `Embedder` and
    `Chatter` (Phase 1) to accept and forward a trace id, a real change to two
    interfaces that have been stable since Phase 1 - out of scope for landing
    Langfuse support itself.
  - Wait for `litellm` to fix the underlying span handling and re-verify.
- `pyproject.toml` gained `langfuse` as a runtime dependency - required because
  `"langfuse_otel"` imports it lazily on first use; without it installed, enabling
  tracing would fail at the first LLM call instead of being visibly absent from
  dependencies.
