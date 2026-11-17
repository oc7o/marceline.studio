"""Which hostnames need hoster intelligence, and filling their records (ADR 0027).

The lookups live in `catalog.hosting`, which may only import `core`. Choosing *which* hosts matter
needs pages, their media hosts and customer findings — `index` and `matching` together — so, like
`marceline.scheduler` and `marceline.safety_incidents`, the selection sits in the project package.

Two rules hold here:

- **Hostnames only.** A media URL is reduced to its host the moment it is read, and nothing else of
  it leaves this module: not the path, not the media id, not the kind. The customer case file shows
  who serves a page's previews, never what they are (#53).
- **Customers first, flagged pages never.** Hosts of pages a customer can see as findings are
  looked up before the plain site list, and only pages `marceline.app.safety` lets a customer see
  contribute hosts. A safety-flagged page's CDN is not looked up for this feature at all.

It also stamps `HosterContact.removed_observed_at` (`observe_notice_removals`): when the content a
notice named was first *observed* gone, from the pages' availability checks and case findings,
never from a typed date — and never more precise than the recheck interval.
"""

import logging
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone

from catalog.hosting.lookup import HostFacts, lookup_host
from catalog.hosting.rdap import RdapClient
from catalog.hosting.services import due_hostnames, failed_facts, normalize_hostname, store_facts
from catalog.models import HosterContact, HostRole, Site
from catalog.request_accounting import RDAP, RequestAccounting
from core.http import PoliteClient, RequestLabel
from index.models import Media, Page
from marceline.app.originals import confirmed_findings_for
from marceline.app.ownership import assigned_references
from marceline.app.safety import page_visible
from matching.models import CaseFinding

logger = logging.getLogger(__name__)

HostLookup = Callable[[str], HostFacts]


def host_of(url: str) -> str:
    try:
        return normalize_hostname(urlsplit(url).hostname or "")
    except ValueError:
        return ""


def content_hosts_by_page(pages: Iterable[Page]) -> dict[int, list[str]]:
    """Per page id: the hosts its previews and stills are served from, sorted, hostnames only.

    The page's own site host is left out (it is the page host), and so are `capture` rows, whose
    URL is the page's own URL (ADR 0016). Pages a customer may not see contribute nothing.
    """
    visible = {page.pk: page for page in pages if page_visible(page)}
    hosts: dict[int, set[str]] = {page_id: set() for page_id in visible}
    rows = (
        Media.objects.filter(page_id__in=list(visible))
        .exclude(kind=Media.Kind.CAPTURE)
        .values_list("page_id", "url")
    )
    for page_id, url in rows:
        host = host_of(url)
        if host and host != normalize_hostname(visible[page_id].site.domain):
            hosts[page_id].add(host)
    return {page_id: sorted(names) for page_id, names in hosts.items()}


def customer_hostnames() -> list[str]:
    """Hosts of every page a customer currently sees as a finding: page host, then its CDNs.

    An original belongs to a customer when an organisation owns it (`ownership`). Which findings
    count is `marceline.app.originals.confirmed_findings_for` — the one definition every customer
    view reads through: confirmed only, flagged pages out. A finding nobody has confirmed yet is not
    shown to a customer, so its hosts are not customer hosts and wait with the plain site list.
    """
    ids = list(assigned_references().values_list("pk", flat=True))
    pages: dict[int, Page] = {}
    for rows in confirmed_findings_for(ids).values():
        for row in rows:
            pages.setdefault(row.page.pk, row.page)
    content = content_hosts_by_page(pages.values())
    names: list[str] = []
    for page_id in sorted(pages):
        names.append(normalize_hostname(pages[page_id].site.domain))
        names.extend(content.get(page_id, []))
    return names


def site_hostnames() -> list[str]:
    domains = Site.objects.filter(enabled=True).order_by("domain").values_list("domain", flat=True)
    return [normalize_hostname(domain) for domain in domains]


def candidate_hostnames() -> list[str]:
    """Customer-visible hosts first, then every enabled site, each once."""
    return list(dict.fromkeys(name for name in (*customer_hostnames(), *site_hostnames()) if name))


def registry_lookup() -> tuple[HostLookup, Callable[[], None]]:
    """A real lookup over DNS and RDAP, identified by the crawler's User-Agent, and its closer."""
    client = PoliteClient(
        settings.CRAWLER_USER_AGENT,
        max_retries=2,
        observer=RequestAccounting(),
        label=RequestLabel(RDAP, role=HostRole.THIRD_PARTY, enforce=False),
    )
    rdap = RdapClient(client)
    return (lambda hostname: lookup_host(hostname, rdap=rdap)), client.close


@dataclass
class LookupReport:
    candidates: int = 0
    due: list[str] = field(default_factory=list)
    results: list[tuple[str, str]] = field(default_factory=list)
    unknown_domain: str | None = None
    dry_run: bool = False


def lookup_hosting(
    *,
    domain: str | None = None,
    limit: int | None = None,
    refresh: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
    lookup_factory: Callable[[], tuple[HostLookup, Callable[[], None]]] | None = None,
) -> LookupReport:
    """Look up every candidate host whose record is missing or due, customer hosts first.

    `domain` narrows the run to one hostname, which must be a candidate: this command does not
    look up arbitrary names. Dry runs make no request and write nothing. Sequential by design;
    the RDAP client spaces requests per registry host.
    """
    now = now or timezone.now()
    report = LookupReport(dry_run=dry_run)
    candidates: Sequence[str] = candidate_hostnames()
    if domain is not None:
        wanted = normalize_hostname(domain)
        if wanted not in candidates:
            report.unknown_domain = domain
            return report
        candidates = [wanted]
    report.candidates = len(candidates)
    due = due_hostnames(candidates, now, refresh=refresh)
    report.due = due[:limit] if limit is not None else due
    if dry_run or not report.due:
        return report
    lookup, close = (lookup_factory or registry_lookup)()
    try:
        for hostname in report.due:
            report.results.append((hostname, _look_up_one(hostname, lookup)))
    finally:
        close()
    return report


def _look_up_one(hostname: str, lookup: HostLookup) -> str:
    """Look up and store one host; a crash is stored as a `failed` record and never ends the run.

    A registry can answer anything. Whatever slipped past `catalog.hosting.rdap`'s defences is
    logged by exception type only (the message may quote the answer) and recorded as
    `lookup_error`, so the host backs off like any failure instead of aborting every run.
    """
    try:
        facts = lookup(hostname)
        record = store_facts(facts, timezone.now())
    except Exception as exc:
        logger.warning("lookup_hosting: %s failed: %s", hostname, type(exc).__name__)
        facts = failed_facts(hostname)
        record = store_facts(facts, timezone.now())
    logger.info("lookup_hosting: %s %s %s", hostname, record.status, ",".join(facts.errors))
    return record.status


def first_removal_after(
    sent_at: datetime,
    page: Page,
    finding_removals: Collection[datetime],
) -> datetime | None:
    """When `page` was first observed gone after `sent_at`, or None (up, unknown, or gone before).

    A case finding's `removed_at` is the first check that found the page gone, and wins; without
    one, a removed page's own `availability_checked_at` stands in — `crawl_recheck` skips removed
    pages, so that is its first removal check too. A page already gone when the notice went out
    tells nothing about the provider and gives no stamp.
    """
    if page.availability != Page.Availability.REMOVED:
        return None
    after = [stamp for stamp in finding_removals if stamp >= sent_at]
    if after:
        return min(after)
    checked = page.availability_checked_at
    return checked if checked is not None and checked >= sent_at else None


def observe_notice_removals() -> int:
    """Stamp `removed_observed_at` on every open notice whose pages are all observed gone.

    The stamp is the latest of the pages' first removals (the content is gone when the last page
    is), written once and never moved: a re-upload is a new case for a new notice. Notices naming
    no page are left alone — nothing can be observed for them. Returns how many were stamped.
    """
    contacts = list(
        HosterContact.objects.filter(removed_observed_at__isnull=True, pages__isnull=False)
        .distinct()
        .prefetch_related("pages")
    )
    page_ids = {page.pk for contact in contacts for page in contact.pages.all()}
    removals: dict[int, list[datetime]] = {}
    rows = CaseFinding.objects.filter(
        page_id__in=page_ids, removed_at__isnull=False, reappeared_at__isnull=True
    ).values_list("page_id", "removed_at")
    for page_id, removed_at in rows:
        if removed_at is not None:  # filtered above; the stubs type the column as nullable
            removals.setdefault(page_id, []).append(removed_at)
    stamped = 0
    for contact in contacts:
        stamps = [
            first_removal_after(contact.sent_at, page, removals.get(page.pk, []))
            for page in contact.pages.all()
        ]
        if not stamps or any(stamp is None for stamp in stamps):
            continue
        contact.removed_observed_at = max(stamp for stamp in stamps if stamp is not None)
        contact.save(update_fields=["removed_observed_at"])
        stamped += 1
    return stamped
