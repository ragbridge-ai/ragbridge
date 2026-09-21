# 10. Documents identified by an external id, unique per tenant

Date: 2026-09-21

## Status

Proposed. See [docs/plans/external-ids.md](../plans/external-ids.md).

## Context

Applications that use ragbridge already have their own records and ids, and need to keep
ragbridge in step with them: create a record, edit it, delete it, retry any of these
safely. Today a document is identified by a UUID that ragbridge chooses, and is unique by
`UNIQUE (tenant_id, sha256)` on its content. So the application must store ragbridge's
UUID, an edit creates a second document, and two different records with identical text
(shared boilerplate, an untranslated copy) cannot both exist.

## Decision

1. **A nullable `documents.external_id` (`VARCHAR(255)`), with
   `UNIQUE (tenant_id, external_id)`.** `NULL` means an ordinary upload, which is every
   existing row. It is per tenant because two applications may both have a `post:42`.
2. **Content uniqueness applies to uploads only:** the old constraint becomes a partial
   unique index, `UNIQUE (tenant_id, sha256) WHERE external_id IS NULL`. Uploads keep
   deduplicating exactly as before. For synced documents `sha256` is a change detector:
   an unchanged hash means no re-chunking and no re-embedding.
3. **New columns `metadata` (`JSONB`, default `{}`), `source_updated_at` and `updated_at`
   (`TIMESTAMPTZ`).** `source_updated_at` lets the service ignore a write that arrives
   after a newer one.
4. **Both unique constraints get explicit names.** The project's naming convention builds
   `uq` names from the first column only, so `(tenant_id, sha256)` and
   `(tenant_id, external_id)` would both be called `uq_documents_tenant_id`. Verified by
   compiling both.
5. **The database constraint is the concurrency guarantee.** Writers use
   `INSERT ... ON CONFLICT DO NOTHING` and then `SELECT ... FOR UPDATE`, so two concurrent
   writes of one id serialise on one row instead of racing a `SELECT` against an `INSERT`.

## Alternatives considered

- **Keep `UNIQUE (tenant_id, sha256)` for every document.** Two records with identical
  text would conflict. Rejecting the second breaks the sync, and sharing one document
  between two ids means deleting one record deletes the other's text.
- **Drop content uniqueness entirely.** It would change `POST /documents`, which clients
  rely on to deduplicate, for no gain to this feature.
- **A separate `external_documents` mapping table.** It adds a join to every lookup and a
  second row to keep consistent, for a one-to-one relation that a column expresses
  directly.
- **A separate `title` column.** The existing `filename` is already the display name in
  sources and in the context label, and holds the title instead.

## Consequences

- Synced and uploaded documents live in one table and are searched identically; no
  retrieval, context, MCP or playground code changes.
- `POST /documents` must deduplicate only against rows with `external_id IS NULL`, or an
  upload could be answered with a synced document that a later sync changes or deletes.
- The migration rewrites no data: every existing row has `external_id IS NULL`, so the
  partial index accepts exactly what the old constraint did. Its downgrade cannot restore
  the old constraint over synced documents that share text, so it refuses while any
  synced document exists.
- A delete leaves no trace, so a delayed older write can re-create a deleted document.
  Tombstones are left for later, if a real client needs them.
