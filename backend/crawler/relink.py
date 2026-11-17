"""Apply credit flags (issue #3) to pages indexed before them: CLI `relink_flagged_pages`.

A re-crawl applies them by itself (`crawler.services.store_parse_result`); this is the backfill
for stored pages, which may never be crawled again. Per site, a classifier re-reads each page's
stored extraction (`STORED_CREDITS`); the page gets the credit keys a crawl would store today and
loses the creator links those flags rule out (`crawler.services.creator_links`). It never adds a
link, never deletes a page and skips pages without a stored extraction (blocked pages).
The report carries counts only: no names, titles or URLs.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from django.db import transaction

from catalog.models import Creator
from crawler.services import cached_creators, creator_links, unlink_creators
from crawler.sites import site03
from index.models import Page

StoredCredits = Callable[[Mapping[str, object]], site03.Credits | None]

# Sites whose adapter can classify stored credits: domain -> classifier of `Page.raw_extraction`.
STORED_CREDITS: dict[str, StoredCredits] = {site03.Site03Adapter.domain: site03.stored_credits}


@dataclass
class RelinkReport:
    domain: str
    dry_run: bool
    pages: int = 0
    without_extraction: int = 0
    credits_changed: int = 0
    flags_written: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    compilations: int = 0
    compilation_reasons: Counter[str] = field(default_factory=Counter)
    pages_unlinked: int = 0
    links_removed: int = 0
    pages_left_without_links: int = 0
    active_pages_left_without_links: int = 0
    creators: set[int] = field(default_factory=set)

    def lines(self) -> list[str]:
        would = "would be " if self.dry_run else ""
        return [
            f"{self.domain}: {self.pages} page(s), {self.without_extraction} without a stored "
            "extraction (skipped)",
            f"credits changed: {self.credits_changed} page(s); dropped credits: "
            f"{_counts(self.dropped)}",
            f"compilations: {self.compilations} page(s); reasons: "
            f"{_counts(self.compilation_reasons)}",
            f"credit flags {would}written: {self.flags_written} page(s)",
            f"creator links {would}removed: {self.links_removed} on {self.pages_unlinked} "
            f"page(s), {len(self.creators)} creator(s)",
            f"pages left without any creator link: {self.pages_left_without_links}, "
            f"{self.active_pages_left_without_links} of them active (targeted indexing keeps "
            "those only when seeded, matched or clustered)",
        ]


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{key} {value}" for key, value in sorted(counter.items())) or "none"


def relink_flagged_pages(
    domain: str, *, dry_run: bool = True, creators: Sequence[Creator] | None = None
) -> RelinkReport:
    """Classify the stored credits of `domain`'s pages and remove the links they rule out.

    Idempotent. With `apply` each page is re-read under a row lock, so a live crawler storing the
    same page at the same time never loses its write.
    """
    classify = STORED_CREDITS.get(domain)
    if classify is None:
        raise ValueError(f"no stored credit classifier for {domain}")
    all_creators = list(creators) if creators is not None else cached_creators()
    report = RelinkReport(domain=domain, dry_run=dry_run)
    pages = Page.objects.filter(site__domain=domain).order_by("pk")
    for page_id in pages.values_list("pk", flat=True):
        with transaction.atomic():
            rows = Page.objects.filter(pk=page_id)
            page = (rows if dry_run else rows.select_for_update()).first()
            if page is not None:
                _relink(page, classify, all_creators, report)
    return report


def _relink(
    page: Page, classify: StoredCredits, creators: Sequence[Creator], report: RelinkReport
) -> None:
    report.pages += 1
    raw = page.raw_extraction if isinstance(page.raw_extraction, dict) else {}
    credits = classify(raw)
    if credits is None:
        report.without_extraction += 1
        return
    performers = list(credits.performers)
    flags = credits.extra()
    report.credits_changed += int(raw.get("performers") != performers)
    report.dropped.update(reason for _, reason in credits.dropped)
    if credits.compilation:
        report.compilations += 1
        report.compilation_reasons.update(credits.compilation)
    changed = raw.get("performers") != performers or any(
        key not in raw or raw[key] != value for key, value in flags.items()
    )
    report.flags_written += int(changed)
    tags = [tag for tag in page.tags if isinstance(tag, str)] if isinstance(page.tags, list) else []
    ruled_out = {
        c.pk for c in creator_links(page.title, tags, performers, flags, creators).ruled_out
    }
    linked = set(page.creators.values_list("pk", flat=True))
    removed = linked & ruled_out
    if removed:
        report.pages_unlinked += 1
        report.links_removed += len(removed)
        report.creators |= removed
        if not linked - removed:
            report.pages_left_without_links += 1
            report.active_pages_left_without_links += int(page.active)
    if report.dry_run:
        return
    if changed:
        Page.objects.filter(pk=page.pk).update(
            raw_extraction={**raw, "performers": performers, **flags}
        )
    unlink_creators(page, sorted(removed))
