# 0002 — PostgreSQL 17 and table-based queues

Status: accepted (2026-09-14)

## Context
Several processes write concurrently (crawler, media, matcher, web), which rules out SQLite.
We need JSON documents, fuzzy title search, and later vector search. Adding Redis + Celery would
add a second stateful system to run and reason about.

## Decision
PostgreSQL 17 is the only datastore. Work queues are ordinary tables (e.g. `CrawlTask`) claimed
with `SELECT … FOR UPDATE SKIP LOCKED`. Use JSONB, `pg_trgm`, and `pgvector` when needed.
PDQ hashes are stored as 32-byte `bytea`; similarity search happens in memory in the matcher,
not in SQL.

## Consequences
- Queue state is inspectable and editable in the Django admin.
- Throughput is bounded by Postgres; fine for thousands of tasks per minute. Celery + Redis is the
  upgrade path if that becomes a limit.
- Tests require a real Postgres (compose), not SQLite.
