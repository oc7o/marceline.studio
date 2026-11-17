# 0050 — A recorded route for a site operator's objection

Status: accepted (2026-09-18)

Issue #108. Procedure: `docs/runbooks/site-objection.md`.

## Context

A **performer** who is not our customer and wants out has a real route: ADR 0038 index suppression,
`docs/suppression.md`, `manage.py suppress_creator`, `Creator.objects.usable()` as the single read
rule, and a database trigger that drops a link any other writer still tries to store.

A **site operator** had nothing. If one writes to us — directly, through a lawyer, or as an abuse
complaint our hosting provider forwards — today's answer is `Site.enabled = False` typed by hand and
no record of who asked, when, on what basis, what we did or who decided it. It is the same shape as
the failure issue #106 records for the capture switches: a policy change nobody can reconstruct.

## Decision

### One row per objection, in the shape of `CreatorSuppression`

`catalog.SiteObjection`: the site, the date it reached us, a **kind** from a closed list
(`operator_request`, `legal_letter`, `provider_complaint`, `policy_change` — the last for a
robots.txt or terms change we noticed ourselves), a **channel**, a short note, the **decision**
(`pending`, `paused`, `disabled`, `media_stopped`, `declined`), the reason, who decided and when,
and when the decision was applied.

**What may never go in it**: the sender's name, address, telephone number, signature block or a
copy of their letter. That correspondence stays in the operator's mailbox, exactly as for
`CreatorSuppression`. The note's help text says so, and the note is for the substance of the
request, not for the person who made it.

**It is a record, so it is not deletable** — no delete permission in the admin, and `PROTECT` on
the site, like `DoNotContactRecord`. The point of writing down who asked us to stop is that the
answer survives the person who gave it.

### The decision is applied through the switches that already exist

`catalog.site_objections.apply_decision` is the one path, and it adds no switch of its own:

* `paused` → `Site.paused_until` 30 days ahead with the objection as `pause_reason`. Reversible,
  applied at once, and the honest answer to "somebody wrote and we have not decided yet". It
  expires on purpose: a pause that lasted for ever would be a disable nobody took.
* `disabled` → `Site.enabled = False`. The task queue filters on it, so no page of the site is
  requested again.
* `media_stopped` → the origin media budget goes to 0: page text still indexed, no picture of the
  site's own hosts fetched. For an objection about bandwidth rather than indexing.
* `declined` → nothing changes, and the reason is required by a database check constraint.

Applying is idempotent, and applying again is not deciding again (the first decider is kept).

### The pages stay

Stopping a crawl is not a deletion. A site operator cannot ask us to delete a rights holder's
evidence, and nothing here touches `index.Page`. What happens to the indexed pages of a site we
stop crawling is ADR 0029's retention question — a site that is no longer crawled produces no new
target links, so its pages are deactivated and scrubbed by the ordinary rule and deleted by
`page_retention` on the ordinary clock.

### Every switch goes through the policy audit

`apply_decision` writes through `catalog.policy_audit.apply_policy_update` (issue #106), so the
database trigger records each change as actor `objection:<pk>` with `reason_text(objection)` as the
reason instead of the anonymous `database` an unattributed write leaves. All three fields it
touches are fields the trigger watches — `enabled` and `daily_media_request_budget` always, and
`paused_until` / `pause_reason` because an actor is set, which is precisely the case those two are
recorded for. The `actor` a caller passes (`operator`, `admin:<user>`) stays on the objection: the
audit gets the objection's id, because the person is in the record that id points at.

Two consequences of that audit belong here. A `Site` that has policy-change rows **cannot be
deleted** — the log is append-only — which is a second, independent reason the site of an objection
outlives the objection. And the Site admin demands a reason for the weighty switches; this path
always supplies one, so a decision applied from the command or the admin action is never an
unexplained change.

## Consequences

* An objection has a written procedure (`docs/runbooks/site-objection.md`) and a place to be
  recorded, and the switch is never flipped without one.
* Staff-only: admin plus `manage.py site_objection`. No customer surface, no public API, no reply
  is ever generated — the runbook's first rule is that we answer with an acknowledgement and never
  with findings, counts or URLs.
* The record is one more thing that must not become a shadow file about a person. The help text,
  this ADR and the runbook all say the same, and the model has no field that could hold a contact
  detail.

## Alternatives considered

* **A `Site.objection_reason` text field.** Rejected: one site can be written to twice, and a field
  that is overwritten is not a record.
* **A new "objected" crawl state.** Rejected: a second thing that stops a crawl is a second thing
  that can be forgotten. `Site.enabled` and the pause already do the work, and there is now exactly
  one caller that changes them for this reason.
* **Deleting the site's pages on request.** Rejected: they are evidence a rights holder asked us to
  gather, the request comes from the party they are evidence against, and nothing in the ADR 0029
  retention rule needs to be bent for it.
