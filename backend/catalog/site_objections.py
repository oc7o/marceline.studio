"""Applying a site operator's objection through the switches that already exist (issue #108).

`catalog.SiteObjection` is the record; this is the one path that turns a recorded decision into a
change to the site. Nothing else may: an objection answered by editing `Site.enabled` by hand is
exactly the state tripwire T6 describes (`docs/legal/crawl-position.md`) — the crawl stops and
nothing says who asked or why.

**Four decisions, three switches, no new one.**

* `paused` — `Site.paused_until` is set `PAUSE_DAYS` ahead with the objection as `pause_reason`.
  Reversible, applied at once, and the answer to "somebody wrote to us and we have not decided
  yet". It expires on purpose: a pause that lasted for ever would be a disable nobody took.
* `disabled` — `Site.enabled = False`. The task queue filters on it, so no page of the site is
  requested again (`crawler/services.py`, the allowlist of § 1.1 of the crawl position).
* `media_stopped` — the origin media budget goes to 0: page text is still indexed, no picture of
  the site's own hosts is fetched. For an objection that is about bandwidth rather than indexing.
* `declined` — nothing changes, and the reason is required by a database constraint.

**The pages stay.** Stopping a crawl is not a deletion. What happens to indexed pages of a site we stop crawling is ADR 0029's
retention question, not this one; the runbook (`docs/runbooks/site-objection.md`) says so and says
what goes to a lawyer instead.

**Every switch is written through the policy audit** (issue #106): `apply_decision` goes through
`catalog.policy_audit.apply_policy_update`, so the database trigger records each change as
`objection:<pk>` with `reason_text(objection)` as the reason instead of the anonymous `database`.
The three fields it touches are all fields the trigger watches — `enabled`,
`daily_media_request_budget` always, and `paused_until` / `pause_reason` because an actor is set,
which is exactly the case those two are recorded for.

Two consequences of that audit worth knowing here: a `Site` that has policy-change rows **cannot be
deleted** (the log is append-only), and the Site admin demands a reason for the weighty switches —
this path always supplies one, so a decision applied from the command or the admin action is never
an unexplained change.
"""

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from catalog import policy_audit
from catalog.models import SiteObjection

#: How long `paused` pauses a site. Long enough that nobody has to answer the same week, short
#: enough that an undecided objection comes back and is decided rather than forgotten.
PAUSE_DAYS = 30

#: The decisions that change nothing about the site.
NO_SWITCH = frozenset({SiteObjection.Decision.PENDING, SiteObjection.Decision.DECLINED})


def actor_for(objection: SiteObjection) -> str:
    """The actor the policy audit records for a change this objection caused."""
    return f"objection:{objection.pk}"


def reason_text(objection: SiteObjection) -> str:
    """The sentence that goes into `pause_reason` and into the audit. Names no person."""
    base = f"site objection #{objection.pk} ({objection.kind}, received {objection.received_on})"
    return f"{base}: {objection.decision_reason}" if objection.decision_reason else base


def changes_for(objection: SiteObjection, *, now: datetime) -> dict[str, Any]:
    """The `Site` fields this decision changes. Pure: the same input always gives the same dict."""
    if objection.decision in NO_SWITCH:
        return {}
    if objection.decision == SiteObjection.Decision.PAUSED:
        return {
            "paused_until": now + timedelta(days=PAUSE_DAYS),
            "pause_reason": reason_text(objection),
        }
    if objection.decision == SiteObjection.Decision.DISABLED:
        return {"enabled": False}
    if objection.decision == SiteObjection.Decision.MEDIA_STOPPED:
        return {"daily_media_request_budget": 0}
    raise ValueError(f"unknown objection decision: {objection.decision}")


def apply_decision(
    objection: SiteObjection, *, actor: str = "", now: datetime | None = None
) -> dict[str, Any]:
    """Apply a recorded decision to the site's switches. Returns what changed (possibly nothing).

    Idempotent: applying the same decision twice changes the same fields to the same values, and a
    `pending` or `declined` objection changes nothing at all. Stamps `applied_at`, and `decided_by`
    / `decided_at` when the decision was taken and neither was filled in.

    `actor` is who asked for it (a username, `operator`, `admin:<user>`) and is kept on the record;
    the **policy audit** is told `objection:<pk>` instead, because that is the fact a reader of
    `catalog.SitePolicyChange` needs — the person is in the objection the id points at.
    """
    moment = now or timezone.now()
    changes = changes_for(objection, now=moment)
    if changes:
        policy_audit.apply_policy_update(
            objection.site,
            actor=actor_for(objection),
            reason=reason_text(objection),
            **changes,
        )
    fields = ["applied_at"]
    objection.applied_at = moment
    if objection.decision != SiteObjection.Decision.PENDING:
        if actor and not objection.decided_by:
            objection.decided_by = actor
            fields.append("decided_by")
        if objection.decided_at is None:
            objection.decided_at = moment
            fields.append("decided_at")
    objection.save(update_fields=fields)
    return changes
