"""The rights model: who may claim a work, on what basis, and how that was checked (ADR 0034).

It gates the future "Prepare notice draft" feature and sends nothing itself. A fingerprint match
says which work a copy contains; it never says who holds the right to that work. This package
records that second fact separately, per organisation and per work (one file = one sha256):

- `models`: claimant, per-work rights record and its immutable transition log, catalogue
  schedules, agency mandates, licensed partners, proof documents, organisation-level events;
- `services`: the only writer — every status change is an explicit staff action, a customer
  self-declaration, or a system consequence (competing claim, mandate revoked or expired);
- `gate`: `notice_gate_blockers(work)`, machine-readable reasons a notice draft may not be prepared;
- `partners`: does a URL belong to a licensed partner;
- `vault`: encrypted proof storage outside every served directory;
- `retention`: what closed records may be deleted, and when.

The tables live in the project app, like `OriginalOwnership` (ADR 0031): they join `accounts`
(the organisation) with works identified by `matching` file digests and the customer ownership
rules, which only the project package may combine.
"""
