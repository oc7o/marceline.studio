"""Who changed a site's policy switches, and why (tripwire T4, issue #106).

`catalog.Site` carries the switches with legal consequences: whether the site is crawled at all,
whether the headless browser may visit it, whether it may click its play control (ADR 0016
amendment, #78), whether the operator approved its age self-declaration cookie (ADR 0007
amendment), the rate we ask it at and the daily budgets. On 2026-09-18 `capture_play_click` went
from false to true on one site with nothing in the system recording who did it or why — a deliberate
operator decision, recorded only on a GitHub issue, noticed only because a measurement was repeated.

**The record is written by a database trigger, not here** (`catalog/migrations/
0024_site_policy_audit.py`). Three reasons, and they decided the design:

1. The flip that started this was a queryset `.update()`, which fires **no** model signal. A
   `post_save` audit would have recorded nothing at all.
2. The same guarantee then holds for the admin, a management command, a Django shell `.save()`,
   a data migration and raw SQL — one writer, one place to read, nothing to keep in step.
3. The project already does it this way where the record must not be optional: the immutable
   invoices (`billing/migrations/0002`), the append-only rights and notice logs, the trigger that
   drops a suppressed creator link.

What this module owns is the **actor**: two transaction-local Postgres settings the trigger reads
(`marceline.policy_actor`, `marceline.policy_reason`). Nothing sets them → the row says `database`,
which is honest and is itself a finding. `policy_actor()` is how the admin, a command or a script
says who it is; `apply_policy_update()` is the checked way to change one of these fields from code.

Values are rendered as text by the trigger. `age_confirmation` is recorded as its **shape** —
`empty` or `approval, N cookie(s)` — never a cookie name or value (root CLAUDE.md: findings name
identifiers, never content).
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from catalog.models import Site, SitePolicyChange

#: The policy-bearing fields the trigger records on every change, whoever writes them.
#: Each one either decides whether we may touch a site at all or how hard we touch it.
POLICY_FIELDS: tuple[str, ...] = (
    "enabled",
    "capture_enabled",
    "capture_play_click",
    "age_confirmation",
    "crawl_delay_s",
    "daily_request_budget",
    "daily_media_request_budget",
)

#: Fields recorded **only when an actor is set**, i.e. when a human changed them. The crawler writes
#: `paused_until` / `pause_reason` on every outage and every reservation pause; logging those would
#: bury the human decisions this table exists for under machine bookkeeping.
ACTOR_ONLY_FIELDS: tuple[str, ...] = ("paused_until", "pause_reason")

#: Changes the admin refuses without a reason. The first three are the weighty policy switches
#: (docs/legal/crawl-position.md); a budget is in the list only
#: when it is **raised**, because lowering it is always allowed and needs no defence.
REASON_REQUIRED_FIELDS: tuple[str, ...] = (
    "capture_play_click",
    "age_confirmation",
    "enabled",
)
REASON_REQUIRED_ON_RAISE: tuple[str, ...] = (
    "daily_request_budget",
    "daily_media_request_budget",
)

ACTOR_SETTING = "marceline.policy_actor"
REASON_SETTING = "marceline.policy_reason"
#: What the trigger writes when nobody said who they were.
UNATTRIBUTED_ACTOR = "database"


@contextmanager
def policy_actor(actor: str, reason: str = "") -> Iterator[None]:
    """Name the actor (and optionally the reason) for every policy change written in this block.

    Opens a transaction, because the two settings are transaction-local: they are gone at COMMIT and
    can never leak into an unrelated write on a pooled connection. Nesting is fine — the inner block
    wins for its own statements and the outer values are restored when it ends.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting(%s, true), current_setting(%s, true)",
                [ACTOR_SETTING, REASON_SETTING],
            )
            row = cursor.fetchone()
            outer = (row[0] or "", row[1] or "") if row else ("", "")
            _set(cursor, actor[:200], reason)
        yield
        # Only on the way out without an error: an exception rolls this block back (or to its
        # savepoint), and a transaction-local setting goes with it. Touching an aborted transaction
        # here would raise over the caller's own exception.
        with connection.cursor() as cursor:
            _set(cursor, *outer)


def _set(cursor: Any, actor: str, reason: str) -> None:
    cursor.execute(
        "SELECT set_config(%s, %s, true), set_config(%s, %s, true)",
        [ACTOR_SETTING, actor, REASON_SETTING, reason],
    )


def apply_policy_update(
    target: Site | QuerySet[Site], *, actor: str, reason: str = "", **fields: Any
) -> int:
    """Change policy fields of one site (or a queryset of them) with the change recorded.

    The checked way to do from code what a bare `Site.objects.filter(...).update(...)` does: the
    trigger records either way, but this is what makes the row say who and why instead of
    `database`. Returns the number of rows updated.
    """
    unknown = set(fields) - set(POLICY_FIELDS) - set(ACTOR_ONLY_FIELDS)
    if unknown:
        raise ValueError(f"not policy fields: {', '.join(sorted(unknown))}")
    if not fields:
        raise ValueError("nothing to change")
    queryset = Site.objects.filter(pk=target.pk) if isinstance(target, Site) else target
    with policy_actor(actor, reason):
        updated = queryset.update(**fields)
    if isinstance(target, Site):
        for name, value in fields.items():
            setattr(target, name, value)
    return updated


@dataclass(frozen=True)
class ChangeSummary:
    """Policy changes over a window: the total and the newest few, for `make status`."""

    total: int
    rows: tuple[SitePolicyChange, ...]


def recent_changes(now: datetime | None = None, *, days: int = 7, limit: int = 5) -> ChangeSummary:
    """Policy switches changed in the last `days`, newest first. Read-only."""
    since = (now or timezone.now()) - timedelta(days=days)
    changes = SitePolicyChange.objects.filter(changed_at__gte=since).select_related("site")
    return ChangeSummary(total=changes.count(), rows=tuple(changes[:limit]))


def age_confirmation_shape(value: Mapping[str, Any] | None) -> str:
    """The shape the trigger records for `Site.age_confirmation`: never a cookie name or value.

    Python's copy of the SQL, for tests and for anything that has to say the same thing twice.
    """
    cookies = (value or {}).get("cookies") or {}
    if not value:
        return "empty"
    return f"approval, {len(cookies)} cookie(s)"


def describe(change: SitePolicyChange) -> str:
    """One line for a report: site, field, old → new, actor, reason. Identifiers only."""
    line = (
        f"site {change.site_id} {change.field}: "
        f"{change.old_value or '-'} → {change.new_value or '-'} "
        f"by {change.actor} at {change.changed_at:%Y-%m-%d %H:%M} UTC"
    )
    return f"{line} — {change.reason}" if change.reason else line
