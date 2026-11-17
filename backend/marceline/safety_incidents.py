"""The cross-app half of the safety incident workflow (issue #66): what is "the same content"?

`index/incidents.py` owns the record and the deduplication *rules*. It may not import `matching`
(module boundaries), so the evidence that two different URLs carry the same content is assembled
here, in the project layer, where both apps are visible. Everything in this module answers
deduplication case (b) — the same content mirrored at another URL or on another site — and nothing
in it fetches, stores or looks at media.

Three sources of evidence, most specific first:

1. **Mirror key** (`matching.services.mirror_key`): the `imgs/{a}/{b}/{15-hex}` poster/frame path
   that site02/site04-style mirrors serve under any host or prefix. Pure URL arithmetic, so
   it works even for a page that was blocked before anything was ever fetched from it.
2. **sha256**: two pages whose media rows carry identical bytes. Only exists where media was
   fetched *before* the page was flagged — a blocked page never fetches anything.
3. **Cluster**: an active `matching.ClusterMember` row. Clusters are built from PDQ frames,
   sha256 and external links, so this is the widest net, but it only covers pages that were
   fingerprinted before they were flagged.

A fourth source is **not** assembled here: the PDQ hashes a case kept from a page it was opened
for (ADR 0011 amendment 2026-09-17). Those belong to `index/incidents.py` alone — they are moved
out of `index.FrameHash` when a page is blocked and no matching code may touch them — so this
module only passes them through as `sync_incidents(hashes=…)`.

What this cannot catch is written down in docs/safety.md; the short version is that a re-encoded
copy that was never fingerprinted before its page was blocked, on a site with its own URL scheme,
has no evidence at all and becomes its own case — which is the safe direction to fail in.
"""

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

from catalog.safety_rules import incident_due
from index.incidents import Decision, Related, RetainedHash, open_or_link, sha256_related
from index.models import Media, Page, SafetyIncidentPage
from matching.models import ClusterMember
from matching.services import mirror_key

logger = logging.getLogger(__name__)

Evidence = SafetyIncidentPage.Evidence
MIRROR_KINDS = (Media.Kind.POSTER, Media.Kind.FRAME)


def mirror_related(page: Page) -> list[Related]:
    """Pages sharing a mirror poster/frame key with this one (deduplication case (b))."""
    keys = {
        key
        for url in Media.objects.filter(page=page, kind__in=MIRROR_KINDS).values_list(
            "url", flat=True
        )
        if (key := mirror_key(url))
    }
    if not keys:
        return []
    page_ids: set[int] = set()
    for key in keys:
        page_ids.update(
            Media.objects.filter(kind__in=MIRROR_KINDS, url__contains=key)
            .exclude(page_id=page.pk)
            .values_list("page_id", flat=True)
        )
    return [Related(page_id=other, evidence=Evidence.MIRROR) for other in sorted(page_ids)]


def cluster_ref(page: Page) -> int | None:
    """The id of the video cluster this page is an active member of, if any."""
    return (
        ClusterMember.objects.filter(page=page)
        .exclude(status=ClusterMember.Status.REJECTED)
        .values_list("cluster_id", flat=True)
        .first()
    )


def cluster_related(page: Page, cluster_id: int | None) -> list[Related]:
    """The other active members of this page's cluster (deduplication case (b))."""
    if cluster_id is None:
        return []
    others = (
        ClusterMember.objects.filter(cluster_id=cluster_id)
        .exclude(status=ClusterMember.Status.REJECTED)
        .exclude(page_id=page.pk)
        .values_list("page_id", flat=True)
    )
    return [Related(page_id=other, evidence=Evidence.CLUSTER) for other in sorted(set(others))]


def related_pages(page: Page, *, cluster_id: int | None = None) -> list[Related]:
    """Every page known to carry this page's content, most specific evidence first.

    Deduplicated by page id: the first (most specific) evidence for a page wins.
    """
    seen: set[int] = set()
    out: list[Related] = []
    for rel in (
        *mirror_related(page),
        *sha256_related(page),
        *cluster_related(page, cluster_id),
    ):
        if rel.page_id in seen:
            continue
        seen.add(rel.page_id)
        out.append(rel)
    return out


@dataclass
class SyncReport:
    dry_run: bool
    scanned: int = 0
    created: int = 0
    settled: int = 0
    linked: Counter[str] = field(default_factory=Counter)
    decisions: list[tuple[int, Decision]] = field(default_factory=list)
    # Blocked by a rule that opens no incident (or not blocked at all): no case.
    not_reportable: int = 0

    @property
    def linked_total(self) -> int:
        return sum(self.linked.values())


def sync_incidents(
    *,
    site_domain: str | None = None,
    pages: Sequence[Page] | None = None,
    hashes: Mapping[int, Sequence[RetainedHash]] | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    limit: int | None = None,
) -> SyncReport:
    """Give every blocked page a case, deduplicated across re-crawls, mirrors and term changes.

    Idempotent: a page that already belongs to an active case only moves its `last_seen_at`, and a
    page whose case a human has settled is left alone. Nothing is written when `dry_run`.

    `hashes` are the PDQ hashes taken off a page at the moment `safety_scan` blocked it
    (deduplication case (d), ADR 0011 amendment 2026-09-17), keyed by page id. They are compared
    against what running cases already keep, and then kept on this page's case. A page with no
    entry simply has none — nothing here fetches or recomputes a hash.

    A page blocked only by rules that open no incident (`auto_block` non-consent wording, the
    `customer_release_queue` "flagged term" rule; `catalog.safety_rules.incident_due`) is skipped:
    the operator decided those are not hotline reports (2026-09-17).
    """
    now = now or timezone.now()
    report = SyncReport(dry_run=dry_run)
    if pages is None:
        queryset = Page.objects.filter(safety_status=Page.SafetyStatus.BLOCKED).select_related(
            "site"
        )
        if site_domain:
            queryset = queryset.filter(site__domain=site_domain)
        selected: Sequence[Page] = list(
            queryset.order_by("pk")[:limit] if limit else queryset.order_by("pk")
        )
    else:
        selected = pages
    for page in selected:
        if page.safety_status != Page.SafetyStatus.BLOCKED or not incident_due(page.safety_matches):
            report.not_reportable += 1
            continue
        report.scanned += 1
        cluster_id = cluster_ref(page)
        decision = open_or_link(
            page,
            related=related_pages(page, cluster_id=cluster_id),
            retained=(hashes or {}).get(page.pk, ()),
            cluster_ref=cluster_id,
            now=now,
            dry_run=dry_run,
        )
        report.decisions.append((page.pk, decision))
        if decision.action == "created":
            report.created += 1
        elif decision.action == "settled":
            report.settled += 1
        else:
            report.linked[decision.evidence] += 1
    return report
