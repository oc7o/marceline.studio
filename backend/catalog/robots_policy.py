"""Record what a site's robots.txt reserves, and pause only the unambiguous case (issue #105).

Tripwire T2 of `docs/legal/crawl-position.md`. `core.robots.parse_robots` answers the fetch's
question — may we take this URL — and to do that it discards every group that does not address us.
Measured on 2026-09-18: no enabled site names our user agent, and two carry
`Disallow: /` groups addressed to **other companies'** named AI-crawler tokens. Those groups are
invisible to the crawler, so today nothing could notice one appearing, changing, or starting to
name us.

`core.robots.describe_robots` is the second reading of the same body — pure, no extra request, no
new request kind, computed from the robots.txt the crawler already refreshes once per origin per
24 h. This module stores it and decides what follows:

* **Names us** (a full-host `Disallow` in a group with our product token) **or a reservation
  header** (`TDM-Reservation`, `X-Robots-Tag: noai`) → unambiguous. The site is paused with reason
  `tdm_reservation` for operator review when `ROBOTS_RESERVATION_PAUSE_ENABLED` is on, and the
  operator is alerted either way. The pause is **not** lifted by the next successful fetch, unlike
  an outage pause (`crawler.services.record_site_reachable`): a legal reservation is not an outage.
* **Names `*`** → already obeyed. `parse_robots` picks the `*` group for us and every URL of the
  site is skipped before a request is made. Recorded and alerted, never paused: there is nothing new
  to enforce, and a test proves the skip.
* **Names only other AI/TDM tokens** → recorded, reported (`request_report`, the Site admin,
  `make status`) and alerted once per new or changed reservation, and **nothing else**. Whether such
  a reservation binds a differently-named TDM actor is not something the code answers: the
  operator decides per site.

Counters and tokens only. The body is never stored; a SHA-256 of it is what tells a change from a
re-read.
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from catalog import robots_alerts
from catalog.models import Site, SiteRobotsPolicy
from core.robots import RobotsFacts

logger = logging.getLogger(__name__)

#: `Site.pause_reason` of a site paused for a reservation. A prefix, not a sentence: the crawler's
#: "the site answered, end its pause" path recognises it and leaves such a pause standing.
TDM_PAUSE_REASON = "tdm_reservation"
#: How long the pause lasts if nobody looks at it. Long on purpose: it ends when the operator ends
#: it, and a month is short enough that a forgotten pause still shows up in a `make status` run.
PAUSE_DAYS = 30


@dataclass(frozen=True)
class Outcome:
    """What recording one robots.txt produced. Booleans and counters only."""

    host: str
    changed: bool
    first_seen: bool
    reserved_against_us: bool
    third_party_reservation: bool
    wildcard_reservation: bool
    paused: bool = False

    @property
    def reserved(self) -> bool:
        return self.reserved_against_us or self.third_party_reservation or self.wildcard_reservation


def detection_enabled() -> bool:
    return bool(getattr(settings, "ROBOTS_RESERVATION_DETECTION_ENABLED", True))


def pause_enabled() -> bool:
    return bool(getattr(settings, "ROBOTS_RESERVATION_PAUSE_ENABLED", False))


def record(
    site_id: int, host: str, facts: RobotsFacts, *, now: datetime | None = None
) -> Outcome | None:
    """Store `facts` for (site, host) and apply the tripwire. None when detection is off.

    Idempotent: a re-read of an unchanged file only moves `checked_at`.
    """
    if not detection_enabled():
        return None
    moment = now or timezone.now()
    with transaction.atomic():
        row, created = SiteRobotsPolicy.objects.select_for_update().get_or_create(
            site_id=site_id,
            host=host.lower(),
            defaults={
                "content_hash": facts.content_hash,
                "agents": list(facts.agents),
                "full_disallow_agents": list(facts.full_disallow_agents),
                "ai_tokens": list(facts.ai_tokens),
                "reservation_headers": list(facts.reservation_headers),
                "names_us": facts.names_us,
                "names_wildcard": facts.names_wildcard,
                "third_party_ai": facts.third_party_reservation,
                "changed_at": moment,
                "checked_at": moment,
            },
        )
        changed = created or row.content_hash != facts.content_hash
        if not created:
            row.content_hash = facts.content_hash
            row.agents = list(facts.agents)
            row.full_disallow_agents = list(facts.full_disallow_agents)
            row.ai_tokens = list(facts.ai_tokens)
            row.reservation_headers = list(facts.reservation_headers)
            row.names_us = facts.names_us
            row.names_wildcard = facts.names_wildcard
            row.third_party_ai = facts.third_party_reservation
            row.checked_at = moment
            if changed:
                row.changed_at = moment
            row.save()
    outcome = Outcome(
        host=host.lower(),
        changed=changed,
        first_seen=created,
        reserved_against_us=facts.reserved_against_us,
        third_party_reservation=facts.third_party_reservation,
        wildcard_reservation=facts.names_wildcard,
    )
    if outcome.reserved_against_us:
        outcome = _apply_pause(site_id, outcome, moment)
    if outcome.reserved and changed:
        robots_alerts.notify(site_id, outcome, now=moment)
    return outcome


def _apply_pause(site_id: int, outcome: Outcome, now: datetime) -> Outcome:
    """Pause the site for review — or only say what would have happened, while the switch is off."""
    if not pause_enabled():
        logger.error(
            "site %s declares a robots.txt reservation naming us on host %s; "
            "ROBOTS_RESERVATION_PAUSE_ENABLED is off, so the site is NOT paused",
            site_id,
            outcome.host,
        )
        return outcome
    reason = (
        f"{TDM_PAUSE_REASON}: this site's robots.txt reserves it against our own user agent "
        f"(host {outcome.host}; the tokens and headers are in Site robots policy). Crawling stays "
        "paused until the operator has reviewed it (tripwire T2, issue #105)."
    )
    Site.objects.filter(pk=site_id).update(
        paused_until=now + timedelta(days=PAUSE_DAYS), pause_reason=reason
    )
    logger.error(
        "site %s paused for %s: its robots.txt reserves it against us", site_id, TDM_PAUSE_REASON
    )
    return replace(outcome, paused=True)


def paused_for_reservation(site: Site, now: datetime | None = None) -> bool:
    """Is this site paused because of a reservation rather than an outage?"""
    moment = now or timezone.now()
    return bool(
        site.paused_until
        and site.paused_until > moment
        and site.pause_reason.startswith(TDM_PAUSE_REASON)
    )


def policy_lines(site: Site, now: datetime | None = None) -> list[str]:
    """Plain-text lines for one site: which hosts reserve what. Tokens only, never a path.

    Used by `manage.py request_report` and the Site admin page, like `media_shapes.shape_lines`.
    """
    rows = [row for row in site.robots_policies.all() if row.reserved]
    lines: list[str] = []
    if paused_for_reservation(site, now):
        lines.append(
            f"  robots reservation: site paused until "
            f"{site.paused_until:%Y-%m-%d %H:%M} UTC pending operator review ({TDM_PAUSE_REASON})"
        )
    for row in rows:
        what = []
        if row.names_us:
            what.append("names our user agent")
        if row.names_wildcard:
            what.append("names `*` (already obeyed: every URL is skipped)")
        if row.third_party_ai:
            what.append(f"third-party AI tokens: {', '.join(row.ai_tokens)}")
        if row.reservation_headers:
            what.append(f"header(s): {', '.join(row.reservation_headers)}")
        lines.append(
            f"  robots reservation on {row.host}: {'; '.join(what)}"
            f" — first seen {row.first_seen_at:%Y-%m-%d}, last changed {row.changed_at:%Y-%m-%d}"
        )
    return lines


@dataclass(frozen=True)
class ReservationCounts:
    """How many sites reserve what, for `make status`."""

    naming_us: int = 0
    wildcard: int = 0
    third_party: int = 0
    paused: int = 0


def counts(now: datetime | None = None) -> ReservationCounts:
    """One count per reservation kind over enabled sites. Read-only."""
    moment = now or timezone.now()
    rows = SiteRobotsPolicy.objects.filter(site__enabled=True)
    return ReservationCounts(
        naming_us=rows.filter(names_us=True).values("site_id").distinct().count(),
        wildcard=rows.filter(names_wildcard=True).values("site_id").distinct().count(),
        third_party=rows.filter(third_party_ai=True).values("site_id").distinct().count(),
        paused=Site.objects.filter(
            enabled=True,
            paused_until__gt=moment,
            pause_reason__startswith=TDM_PAUSE_REASON,
        ).count(),
    )
