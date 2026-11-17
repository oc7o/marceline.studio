"""Safety rule consequences that need the project layer (operator decisions 2026-09-17).

`crawler` stores the verdict (catalog/safety_rules.py says what each action means). Two
consequences need apps the crawler may not import, so they live here:

1. **Incidents for `auto_block_and_incident` pages** ("lolita", the minor terms): opened through
   `marceline.safety_incidents.sync_incidents`, with its full mirror/sha256/cluster deduplication.
   Nothing is sent anywhere; the operator reports by hand (docs/safety.md).
2. **The customer release queue** for `customer_release_queue` pages ("flagged term"). Such a page is
   blocked. Only when it credits a *customer creator* — a rights holder of an organisation-owned
   original (`marceline.targets`, ADR 0029) — is it queued for staff, who see text only and may
   release it for fingerprinting or keep it blocked. Co-performers, watchlist entries and
   prospects are not customer creators unless they are themselves a customer's rights holder.

A release is a logged human decision that makes the page `clear` (reviewed) with the release
terms as the reviewed matches. `page_media_allowed` then passes only through the ordinary rule
(clear and scored clearly safe), and only while no other safety term matches: the release refuses
when any other term matches now, and `crawler.safety.resolve` blocks the page again on any new hit
at a re-crawl, which revokes the release (`crawler.services._safety_fields`).

Entry points: `after_store` (receiver of `index.signals.safety_rules_applied`), `sync_release_queue`
(scheduler job and `safety_scan`), `release_pages` / `keep_blocked` (review API). Nothing here
fetches, stores or looks at media.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from catalog import safety_rules
from catalog.models import Creator
from crawler import safety
from crawler.services import (
    creator_links,
    load_safety_terms,
    match_creators,
    page_texts,
    recheck_page_ids,
    record_rule_set,
    rescore_page,
)
from index.breadcrumbs import record_page_links
from index.models import Page, SafetyRuleDecision
from index.services import set_safety_status
from marceline.safety_incidents import sync_incidents
from marceline.targets import TargetKind, targets

logger = logging.getLogger(__name__)

BLOCKED = Page.SafetyStatus.BLOCKED
Release = Page.SafetyRelease
Kind = SafetyRuleDecision.Kind


# --- customer credit -------------------------------------------------------------------------


def customer_creators() -> list[Creator]:
    """Rights holders of organisation-owned originals: the only creators whose credit queues a
    `customer_release_queue` page. Deliberately not co-performers, watchlist or prospects."""
    return [target.creator for target in targets() if target.kind == TargetKind.RIGHTS_HOLDER]


@dataclass(frozen=True)
class Credit:
    creator: Creator
    matched_in: tuple[str, ...]  # "performers", "tags", "title"


def _performers(page: Page) -> list[str]:
    raw = page.raw_extraction if isinstance(page.raw_extraction, Mapping) else {}
    performers = raw.get("performers")
    return [p for p in performers if isinstance(p, str)] if isinstance(performers, list) else []


def _tags(page: Page) -> list[str]:
    return [tag for tag in page.tags if isinstance(tag, str)] if isinstance(page.tags, list) else []


def customer_credits(page: Page, creators: Sequence[Creator]) -> list[Credit]:
    """Customer creators the page credits, by the crawler's own linking rule (`creator_links`:
    whole-word names in title, tags or performer credits; compilations credit nobody)."""
    raw = page.raw_extraction if isinstance(page.raw_extraction, Mapping) else {}
    performers, tags = _performers(page), _tags(page)
    linked = creator_links(page.title, tags, performers, raw, creators).linked
    credits: list[Credit] = []
    for creator in sorted(linked, key=lambda c: (c.name, c.pk)):
        where = tuple(
            name
            for name, texts in (("performers", performers), ("tags", tags), ("title", [page.title]))
            if match_creators(texts, [creator])
        )
        credits.append(Credit(creator, where))
    return credits


# --- the queue ---------------------------------------------------------------------------------


@dataclass
class ReleaseQueueReport:
    dry_run: bool
    candidates: int = 0
    queued: int = 0  # credited and waiting for staff (already or newly)
    newly_queued: int = 0
    uncredited: int = 0  # stays blocked: no customer credit
    other_hits: int = 0  # stays blocked: another safety rule matched too
    staff_kept_blocked: int = 0  # a human already decided to keep it blocked
    dequeued: int = 0


def _candidates() -> list[Page]:
    return list(
        Page.objects.select_related("site")
        .filter(
            Q(
                safety_status=BLOCKED,
                safety_matches__contains=[{"action": "customer_release_queue"}],
            )
            | Q(safety_release=Release.QUEUED)
        )
        .order_by("pk")
    )


def _outcome(page: Page, creators: Sequence[Creator]) -> tuple[str, Credit | None]:
    """Where a candidate page belongs: queued | uncredited | other_hits | staff_kept_blocked."""
    if page.safety_status != BLOCKED:
        return "not_blocked", None
    if page.safety_release == Release.KEPT_BLOCKED:
        return "staff_kept_blocked", None
    if not safety_rules.release_queue_only(page.safety_matches):
        return "other_hits", None
    credits = customer_credits(page, creators)
    if not credits:
        return "uncredited", None
    return "queued", credits[0]


def sync_release_queue(
    *,
    pages: Sequence[Page] | None = None,
    creators: Sequence[Creator] | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ReleaseQueueReport:
    """Queue credited `customer_release_queue` pages for staff and take out those that no longer
    belong (another hit, no credit any more, unblocked). Idempotent; nothing written in a dry run.

    `pages` may be unsaved copies carrying a verdict not stored yet (a `safety_scan` dry run).
    Writes re-read each page under a row lock and decide from that row, never from `pages`.
    """
    now = now or timezone.now()
    creators = customer_creators() if creators is None else list(creators)
    report = ReleaseQueueReport(dry_run=dry_run)
    for page in _candidates() if pages is None else pages:
        report.candidates += 1
        outcome, credit = _outcome(page, creators) if dry_run else _apply(page.pk, creators, now)
        if outcome == "queued" and credit is not None:
            report.queued += 1
            if page.safety_release != Release.QUEUED:
                report.newly_queued += 1
        elif outcome == "uncredited":
            report.uncredited += 1
        elif outcome == "other_hits":
            report.other_hits += 1
        elif outcome == "staff_kept_blocked":
            report.staff_kept_blocked += 1
        if outcome != "queued" and page.safety_release == Release.QUEUED:
            report.dequeued += 1
    return report


def _apply(page_id: int, creators: Sequence[Creator], now: datetime) -> tuple[str, Credit | None]:
    with transaction.atomic():
        page = Page.objects.select_for_update(no_key=True, of=("self",)).filter(pk=page_id).first()
        if page is None:
            return "gone", None
        outcome, credit = _outcome(page, creators)
        if outcome == "queued" and credit is not None:
            if (
                page.safety_release != Release.QUEUED
                or page.safety_release_creator_id != credit.creator.pk
            ):
                was_queued = page.safety_release == Release.QUEUED
                _set_release(page, Release.QUEUED, creator=credit.creator, now=now)
                if not was_queued:
                    _log(page, Kind.QUEUED, creator=credit.creator)
        elif page.safety_release == Release.QUEUED:
            _set_release(page, Release.NONE, creator=None, now=now)
            _log(page, Kind.DEQUEUED, note=outcome)
    return outcome, credit


def _set_release(page: Page, state: str, *, creator: Creator | None, now: datetime) -> None:
    page.safety_release = state
    page.safety_release_creator = creator
    page.safety_release_at = now
    page.save(update_fields=("safety_release", "safety_release_creator", "safety_release_at"))


def _log(
    page: Page, kind: str, *, creator: Creator | None = None, by: str = "", note: str = ""
) -> None:
    SafetyRuleDecision.objects.create(
        page=page,
        kind=kind,
        status=page.safety_status,
        terms=page.safety_matches if isinstance(page.safety_matches, list) else [],
        rule_version=page.safety_rule_version,
        creator=creator,
        decided_by=by,
        note=note,
    )


# --- staff decisions ---------------------------------------------------------------------------


class ReleaseRefused(Exception):
    def __init__(self, page_id: int, reason: str) -> None:
        super().__init__(f"page {page_id}: {reason}")
        self.page_id = page_id
        self.reason = reason


def _locked_queued(page_ids: Sequence[int]) -> list[Page]:
    pages = list(
        Page.objects.select_for_update(of=("self",))
        .select_related("site")
        .filter(pk__in=page_ids)
        .order_by("pk")
    )
    for page in pages:
        if page.safety_status != BLOCKED or page.safety_release != Release.QUEUED:
            raise ReleaseRefused(page.pk, "not in the customer release queue")
    return pages


def release_pages(page_ids: Sequence[int], *, by: str, note: str) -> list[Page]:
    """Staff release queued pages for fingerprinting. All or nothing.

    Re-checked under a row lock from the page's *current* text and the *current* rules: every
    matched term must still be a `customer_release_queue` term (any other hit → refused, the page
    stays blocked) and the page must still credit a customer creator. The release makes the page
    clear and reviewed for exactly those terms; the media gate then decides as for any page, and
    a detail re-crawl is queued so the crawler can attach its media.
    """
    now = timezone.now()
    terms = load_safety_terms()
    creators = customer_creators()
    # Releasing a page links its creators again; `usable()` keeps suppressed names out of that
    # (docs/suppression.md), exactly as the crawler's own linking does.
    everyone = list(Creator.objects.usable())
    with transaction.atomic():
        pages = _locked_queued(page_ids)
        for page in pages:
            verdict = safety.check(page_texts(page), terms)
            if not verdict.release_queue_only or not safety_rules.release_queue_only(
                page.safety_matches
            ):
                raise ReleaseRefused(page.pk, "another safety term matches")
            credits = customer_credits(page, creators)
            if not credits:
                raise ReleaseRefused(page.pk, "no customer creator credited")
            page.safety_matches = safety.matches_json(verdict.matches)
            page.safety_rule_version = record_rule_set(terms)
            page.save(update_fields=("safety_matches", "safety_rule_version"))
            _set_release(page, Release.RELEASED, creator=credits[0].creator, now=now)
            set_safety_status(page, Page.SafetyStatus.CLEAR, note=note, now=now, log_kind=None)
            _log(page, Kind.RELEASED, creator=credits[0].creator, by=by, note=note)
            rescore_page(page, safety_terms=terms, now=now)
            raw = page.raw_extraction if isinstance(page.raw_extraction, Mapping) else {}
            links = creator_links(page.title, _tags(page), _performers(page), raw, everyone)
            page.creators.add(*links.linked)
            record_page_links(page, [creator.pk for creator in links.linked], now=now)
        ids = [page.pk for page in pages]
        # A blocked page kept no media rows; the crawler attaches them on its next detail fetch.
        transaction.on_commit(lambda: recheck_page_ids(ids))
    return pages


def keep_blocked(page_ids: Sequence[int], *, by: str, note: str = "") -> list[Page]:
    """Staff keep queued pages blocked. A human decision: it stands on later crawls."""
    now = timezone.now()
    with transaction.atomic():
        pages = _locked_queued(page_ids)
        for page in pages:
            _set_release(page, Release.KEPT_BLOCKED, creator=page.safety_release_creator, now=now)
            set_safety_status(page, BLOCKED, note=note, now=now, log_kind=None)
            _log(page, Kind.KEPT_BLOCKED, creator=page.safety_release_creator, by=by, note=note)
    return pages


def queued_count() -> int:
    return Page.objects.filter(safety_status=BLOCKED, safety_release=Release.QUEUED).count()


# --- after a crawl stored pages ----------------------------------------------------------------


def follow_up(page_ids: Sequence[int]) -> None:
    """Open/update incidents for incident-rule blocks and (re)decide the release queue."""
    pages = list(Page.objects.select_related("site").filter(pk__in=list(page_ids)).order_by("pk"))
    incident_pages = [
        page
        for page in pages
        if page.safety_status == BLOCKED and safety_rules.opens_incident(page.safety_matches)
    ]
    if incident_pages:
        sync_incidents(pages=incident_pages)
    release = [
        page
        for page in pages
        if (
            page.safety_status == BLOCKED
            and safety_rules.has_release_queue_match(page.safety_matches)
        )
        or page.safety_release == Release.QUEUED
    ]
    if release:
        sync_release_queue(pages=release)


def after_store(sender: Any, page_ids: Sequence[int] = (), **kwargs: Any) -> None:
    """Receiver of `index.signals.safety_rules_applied` (connected in `marceline.apps`)."""
    follow_up(page_ids)
