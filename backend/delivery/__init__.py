"""Production as a read-through cache in front of the development VPS (ADR 0028).

This app holds the transport and storage half of that design and nothing that knows what a
customer may see: signed envelopes (`envelope`), the export service token (`tokens`), the cache of
signed per-organisation bundles with its TTL, stale-while-error and single-flight rules (`store`),
the purge receiver's checks (`purge`), the HTTP calls between the two machines (`client`) and the
development-side purge outbox. What goes *into* a bundle is rendered by the customer API code in
the project package (`marceline/export.py`), so tenancy and safety stay single-sourced there.

Depends on `core` only (import-linter).
"""
