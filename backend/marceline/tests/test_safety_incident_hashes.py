"""A blocked page's hash lives on its case and nowhere else (ADR 0011 amendment 2026-09-17).

`safety_scan` still deletes the frame hashes of every page it blocks. For a page blocked by a rule
that opens a hotline case, the hashes are *moved* onto the incident on the way out, so the same
material found again is one case and one report instead of two.

The guarantee these tests exist for is the narrow one: the hash must be reachable for exactly one
decision — "is this the same material as an existing case?" — and unreachable for matching. So
they assert both halves: the case keeps it, `index.FrameHash` does not have it, no page-frame
query returns it, and no module in `matching/` so much as names the table.
"""

from collections.abc import Callable
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import timezone

from catalog.models import Site
from index import incidents
from index.models import FrameHash, Media, Page, SafetyIncident, SafetyIncidentHash
from matching import services as matching_services

MakePage = Callable[[str], Page]
Evidence = incidents.Evidence
#: A term seeded as `auto_block_and_incident` (catalog/0017_safety_rule_actions): a CSAM suspect,
#: which is the only kind of block that opens a case.
INCIDENT_TERM = "underage"


def fingerprinted(page: Page, pdq: bytes, *, quality: int = 90) -> None:
    """A page as the crawler left it while it was still clear: media fetched, one frame hashed."""
    media = Media.objects.create(
        page=page,
        kind=Media.Kind.POSTER,
        url=f"https://cdn.example.test/{page.pk}.jpg",
        status=Media.Status.DONE,
        eligible_at=timezone.now(),
        fetched_at=timezone.now(),
    )
    FrameHash.objects.create(media=media, frame_index=0, pdq=pdq, quality=quality)


def flag(page: Page) -> None:
    """Make the page's own text match an incident-opening term, as a term edit or re-crawl would."""
    Page.objects.filter(pk=page.pk).update(raw_extraction={"keywords": [INCIDENT_TERM]})


def near(pdq: bytes, bits: int) -> bytes:
    value = int.from_bytes(pdq, "big")
    for bit in range(bits):
        value ^= 1 << bit
    return value.to_bytes(32, "big")


def scan(*args: str) -> str:
    out = StringIO()
    call_command("safety_scan", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def blocked_with_hash(make_page: MakePage) -> tuple[Page, bytes]:
    """One page that was fingerprinted, then blocked by a term edit — the duplicate-report case."""
    page = make_page("flagged")
    pdq = bytes(range(32))
    fingerprinted(page, pdq)
    flag(page)
    return page, pdq


def test_the_scan_moves_the_hash_onto_the_case_and_still_deletes_it(
    blocked_with_hash: tuple[Page, bytes],
) -> None:
    page, pdq = blocked_with_hash

    output = scan()

    assert Page.objects.get(pk=page.pk).safety_status == Page.SafetyStatus.BLOCKED
    assert "frame hashes deleted: 1 (of them kept on a case: 1)" in output
    assert FrameHash.objects.count() == 0
    incident = SafetyIncident.objects.get()
    kept = list(SafetyIncidentHash.objects.filter(incident=incident))
    assert [bytes(row.pdq) for row in kept] == [pdq]

    # Idempotent: a second scan neither duplicates the row nor opens a second case.
    scan()
    assert SafetyIncidentHash.objects.count() == 1
    assert SafetyIncident.objects.count() == 1


def test_a_dry_run_keeps_nothing_and_says_what_it_would_keep(
    blocked_with_hash: tuple[Page, bytes],
) -> None:
    output = scan("--dry-run")

    assert "frame hashes to delete: 1 (of them to keep on a case: 1)" in output
    assert SafetyIncidentHash.objects.count() == 0
    assert FrameHash.objects.count() == 1


def test_a_re_encoded_mirror_blocked_later_joins_the_same_case(
    blocked_with_hash: tuple[Page, bytes],
    make_page: MakePage,
) -> None:
    """The point of the whole change: one case, one hotline report."""
    _, pdq = blocked_with_hash
    scan()

    other_site = Site.objects.create(domain="mirror.test", name="Mirror")
    mirror = Page.objects.create(
        site=other_site, url="https://mirror.test/copy", title="copy", safety_score=1.0
    )
    fingerprinted(mirror, near(pdq, 4))
    flag(mirror)

    scan()

    assert SafetyIncident.objects.count() == 1
    rows = SafetyIncident.objects.get().incident_pages.order_by("pk")
    assert [row.evidence for row in rows] == [Evidence.ORIGIN, Evidence.PDQ]
    assert SafetyIncidentHash.objects.count() == 2


def test_a_blocked_page_with_no_stored_hash_still_gets_its_case(make_page: MakePage) -> None:
    page = make_page("text-only")
    flag(page)

    output = scan()

    assert "frame hashes deleted: 0 (of them kept on a case: 0)" in output
    assert SafetyIncident.objects.get().incident_pages.get().page_id == page.pk
    assert SafetyIncidentHash.objects.count() == 0


def test_a_page_blocked_by_a_rule_without_a_case_keeps_no_hash(make_page: MakePage) -> None:
    """`auto_block` wording (no incident) and the release queue are not reporting cases, so
    nothing about them is retained: no case exists to hold it."""
    page = make_page("auto-blocked")
    fingerprinted(page, bytes(range(32)))
    Page.objects.filter(pk=page.pk).update(raw_extraction={"keywords": ["rape"]})

    output = scan()

    assert Page.objects.get(pk=page.pk).safety_status == Page.SafetyStatus.BLOCKED
    assert "frame hashes deleted: 1 (of them kept on a case: 0)" in output
    assert (SafetyIncident.objects.count(), SafetyIncidentHash.objects.count()) == (0, 0)
    assert FrameHash.objects.count() == 0


def test_the_kept_hash_never_reaches_a_matching_query(
    blocked_with_hash: tuple[Page, bytes],
) -> None:
    page, pdq = blocked_with_hash

    scan()

    assert SafetyIncidentHash.objects.filter(incident__isnull=False).count() == 1
    # The two ways the matcher ever sees a page frame: the loader and the table itself.
    assert matching_services.load_page_hashes() == {}
    assert matching_services.load_page_hashes([page.pk]) == {}
    assert not FrameHash.objects.filter(pdq=pdq).exists()
    assert FrameHash.objects.count() == 0


def test_no_matching_module_knows_the_table() -> None:
    """Structural, because the guarantee is architectural: the matcher must not grow a read.

    `index.FrameHash` is what every matcher loads; these hashes are deliberately somewhere else,
    and `matching` may not learn about that place — not even to "improve" a match.
    """
    root = Path(matching_services.__file__).resolve().parent
    offenders = [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*.py"))
        if "SafetyIncidentHash" in path.read_text() or "retained_hashes" in path.read_text()
    ]

    assert offenders == []


def test_retention_takes_the_kept_hash_with_the_case(
    blocked_with_hash: tuple[Page, bytes],
) -> None:
    scan()
    now = timezone.now()
    incident = SafetyIncident.objects.get()
    incidents.mark_false_positive(incident, by="tester", now=now - timedelta(days=400))
    assert SafetyIncidentHash.objects.count() == 1

    call_command("safety_retention", stdout=StringIO())

    assert SafetyIncident.objects.count() == 0
    assert SafetyIncidentHash.objects.count() == 0
