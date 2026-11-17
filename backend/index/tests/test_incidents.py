"""Safety incidents: deduplication (the four cases) and retention (docs/safety.md, issue #66)."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from django.forms.models import model_to_dict
from django.utils import timezone

from catalog.models import Site
from index import incidents
from index.models import (
    FrameHash,
    Media,
    Page,
    SafetyIncident,
    SafetyIncidentHash,
    SafetyIncidentPage,
)
from index.services import cancel_page_media

Evidence = SafetyIncidentPage.Evidence
TERM = {"category": "minor", "term": "malolet", "action": "block"}
OTHER_TERM = {"category": "minor", "term": "shkolnic", "action": "block"}


@pytest.fixture
def make_blocked(site: Site) -> Callable[..., Page]:
    def _make(
        slug: str,
        *,
        matches: Sequence[dict[str, str]] = (TERM,),
        page_site: Site | None = None,
    ) -> Page:
        page_site = page_site or site
        return Page.objects.create(
            site=page_site,
            url=f"https://{page_site.domain}/{slug}",
            title=f"title of {slug}",
            tags=["tag"],
            safety_status=Page.SafetyStatus.BLOCKED,
            safety_matches=list(matches),
            safety_flagged_at=timezone.now(),
        )

    return _make


def test_a_blocked_page_opens_one_case(make_blocked: Callable[..., Page]) -> None:
    page = make_blocked("one")
    decision = incidents.open_or_link(page)

    assert decision.action == "created"
    assert decision.incident is not None
    assert decision.incident.status == SafetyIncident.Status.OPEN
    assert decision.incident.category == "minor"
    assert decision.incident.term == "malolet"
    link = decision.incident.incident_pages.get()
    assert (link.url, link.evidence, link.site_domain) == (
        page.url,
        Evidence.ORIGIN,
        "example.test",
    )


def test_incident_carries_no_page_content(make_blocked: Callable[..., Page]) -> None:
    """URL, timestamps, term and metadata only (docs/safety.md)."""
    page = make_blocked("two")
    incidents.open_or_link(page)

    field_names = {field.name for field in SafetyIncident._meta.get_fields()}
    assert not field_names & {"title", "tags", "text", "raw_extraction", "media", "sha256"}
    values = " ".join(str(value) for value in model_to_dict(SafetyIncident.objects.get()).values())
    assert page.title not in values


def test_same_url_on_a_recrawl_is_not_a_new_case(make_blocked: Callable[..., Page]) -> None:
    """Deduplication case (a)."""
    page = make_blocked("three")
    first = incidents.open_or_link(page)
    later = timezone.now() + timedelta(days=1)

    again = incidents.open_or_link(page, now=later)

    assert again.action == "linked"
    assert again.evidence == Evidence.SAME_URL
    assert again.incident_id == first.incident_id
    assert SafetyIncident.objects.count() == 1
    assert SafetyIncident.objects.get().last_seen_at == later


def test_identical_media_bytes_link_a_mirror_to_the_same_case(
    make_blocked: Callable[..., Page],
) -> None:
    """Deduplication case (b), the part `index` can decide on its own."""
    first_page = make_blocked("four")
    mirror = make_blocked("four-mirror")
    for page in (first_page, mirror):
        Media.objects.create(
            page=page,
            kind=Media.Kind.POSTER,
            url=f"https://cdn.example.test/{page.pk}.jpg",
            sha256="a" * 64,
        )
    first = incidents.open_or_link(first_page)

    decision = incidents.open_or_link(mirror, related=incidents.sha256_related(mirror))

    assert (decision.action, decision.evidence) == ("linked", Evidence.SHA256)
    assert decision.incident_id == first.incident_id
    assert SafetyIncident.objects.count() == 1
    assert sorted(
        SafetyIncident.objects.get().incident_pages.values_list("url", flat=True)
    ) == sorted([first_page.url, mirror.url])


def test_a_new_term_on_an_open_case_is_merged_not_duplicated(
    make_blocked: Callable[..., Page],
) -> None:
    """Deduplication case (c) while the case is still running."""
    page = make_blocked("five")
    first = incidents.open_or_link(page)
    page.safety_matches = [TERM, OTHER_TERM]

    decision = incidents.open_or_link(page)

    assert (decision.action, decision.evidence) == ("linked", Evidence.TERM_CHANGE)
    assert decision.incident_id == first.incident_id
    assert SafetyIncident.objects.get().terms == [
        {"category": "minor", "term": "malolet"},
        {"category": "minor", "term": "shkolnic"},
    ]


def test_a_settled_case_is_not_reopened_for_a_term_it_already_judged(
    make_blocked: Callable[..., Page],
) -> None:
    """Deduplication case (c) after a human decision — docs/safety.md's standing-decision rule."""
    page = make_blocked("six")
    decision = incidents.open_or_link(page)
    assert decision.incident is not None
    incidents.mark_false_positive(decision.incident, by="tester")

    again = incidents.open_or_link(page)

    assert again.action == "settled"
    assert SafetyIncident.objects.count() == 1


def test_a_term_the_decision_never_judged_opens_a_new_case(
    make_blocked: Callable[..., Page],
) -> None:
    page = make_blocked("seven")
    decision = incidents.open_or_link(page)
    assert decision.incident is not None
    incidents.mark_closed(decision.incident, by="tester")
    page.safety_matches = [OTHER_TERM]

    again = incidents.open_or_link(page)

    assert again.action == "created"
    assert again.incident_id != decision.incident_id
    assert SafetyIncident.objects.count() == 2


def test_dry_run_writes_nothing(make_blocked: Callable[..., Page]) -> None:
    page = make_blocked("eight")

    decision = incidents.open_or_link(page, dry_run=True)

    assert (decision.action, decision.incident) == ("created", None)
    assert SafetyIncident.objects.count() == 0


def test_months_ago_clamps_the_day() -> None:
    assert incidents.months_ago(datetime(2026, 3, 31, tzinfo=UTC), 1) == datetime(
        2026, 2, 28, tzinfo=UTC
    )
    assert incidents.months_ago(datetime(2026, 1, 15, tzinfo=UTC), 12) == datetime(
        2025, 1, 15, tzinfo=UTC
    )


def test_retention_closes_a_stale_report_then_deletes_it_after_twelve_months(
    make_blocked: Callable[..., Page],
) -> None:
    now = timezone.now()
    page = make_blocked("nine")
    decision = incidents.open_or_link(page, now=now)
    assert decision.incident is not None
    incidents.mark_reported(
        decision.incident, via="hotline form", reported_at=now - timedelta(days=120)
    )

    planned = incidents.apply_retention(now=now, dry_run=True)
    assert (planned.closed, planned.deleted) == (1, 0)
    assert SafetyIncident.objects.get().status == SafetyIncident.Status.REPORTED

    applied = incidents.apply_retention(now=now)
    assert (applied.closed, applied.deleted) == (1, 0)
    incident = SafetyIncident.objects.get()
    assert incident.status == SafetyIncident.Status.CLOSED
    assert incident.closed_at == now
    assert "Closed automatically" in incident.note

    assert incidents.apply_retention(now=now).closed == 0  # idempotent

    later = now + timedelta(days=370)
    assert incidents.apply_retention(now=later, dry_run=True).deleted == 1
    assert SafetyIncident.objects.count() == 1
    report = incidents.apply_retention(now=later)
    assert (report.deleted, report.deleted_ids) == (1, [incident.pk])
    assert SafetyIncident.objects.count() == 0
    assert SafetyIncidentPage.objects.count() == 0
    assert incidents.apply_retention(now=later).deleted == 0  # idempotent


def test_retention_never_touches_an_open_case(make_blocked: Callable[..., Page]) -> None:
    now = timezone.now()
    old = now - timedelta(days=900)
    page = make_blocked("ten")
    decision = incidents.open_or_link(page, now=old)
    assert decision.incident is not None
    SafetyIncident.objects.filter(pk=decision.incident.pk).update(detected_at=old, last_seen_at=old)

    report = incidents.apply_retention(now=now)

    assert (report.closed, report.deleted, report.open_unreported) == (0, 0, 1)
    assert SafetyIncident.objects.get().status == SafetyIncident.Status.OPEN


def test_a_false_positive_is_deleted_twelve_months_after_the_decision(
    make_blocked: Callable[..., Page],
) -> None:
    now = timezone.now()
    page = make_blocked("eleven")
    decision = incidents.open_or_link(page, now=now)
    assert decision.incident is not None
    incidents.mark_false_positive(decision.incident, by="tester", now=now - timedelta(days=400))

    assert incidents.apply_retention(now=now).deleted == 1
    assert SafetyIncident.objects.count() == 0


# --- Deduplication case (d): the hash a case keeps (ADR 0011 amendment 2026-09-17) ---


def hashed(page: Page, pdq: bytes, *, quality: int = 90) -> Media:
    """One stored frame hash on a page, as a clear page would have had before it was blocked."""
    media = Media.objects.create(
        page=page, kind=Media.Kind.POSTER, url=f"https://cdn.example.test/{page.pk}.jpg"
    )
    FrameHash.objects.create(media=media, frame_index=0, pdq=pdq, quality=quality)
    return media


def block(page: Page) -> list[incidents.RetainedHash]:
    """What a block does: read the hashes, then delete them from `FrameHash` (as the scan does)."""
    retained = incidents.collect_page_hashes([page.pk]).get(page.pk, [])
    cancel_page_media([page.pk])
    return retained


def near(pdq: bytes, bits: int) -> bytes:
    """`pdq` with `bits` bits flipped, i.e. at Hamming distance `bits` from it."""
    value = int.from_bytes(pdq, "big")
    for bit in range(bits):
        value ^= 1 << bit
    return value.to_bytes(32, "big")


def test_a_blocked_pages_hash_moves_out_of_framehash_onto_the_case(
    make_blocked: Callable[..., Page],
) -> None:
    page = make_blocked("hash-origin")
    pdq = bytes(range(32))
    hashed(page, pdq)

    retained = block(page)
    decision = incidents.open_or_link(page, retained=retained)

    assert FrameHash.objects.count() == 0
    assert decision.incident is not None
    kept = list(SafetyIncidentHash.objects.filter(incident=decision.incident))
    assert [(bytes(row.pdq), row.quality, row.source_kind) for row in kept] == [
        (pdq, 90, Media.Kind.POSTER)
    ]


def test_a_re_encoded_mirror_joins_the_case_by_its_retained_hash(
    make_blocked: Callable[..., Page],
) -> None:
    """The gap this closes: no shared URL, no shared bytes, no cluster — only the picture."""
    first_page = make_blocked("hash-first")
    hashed(first_page, bytes(range(32)))
    first = incidents.open_or_link(first_page, retained=block(first_page))

    other_site = Site.objects.create(domain="mirror.test", name="Mirror")
    mirror = make_blocked("hash-mirror", page_site=other_site)
    hashed(mirror, near(bytes(range(32)), 3))

    decision = incidents.open_or_link(mirror, retained=block(mirror))

    assert (decision.action, decision.evidence) == ("linked", Evidence.PDQ)
    assert decision.incident_id == first.incident_id
    assert SafetyIncident.objects.count() == 1
    assert sorted(
        SafetyIncident.objects.get().incident_pages.values_list("url", flat=True)
    ) == sorted([first_page.url, mirror.url])
    # The mirror's own hash is kept too, so the next copy is recognised from either picture.
    assert SafetyIncidentHash.objects.count() == 2


def test_a_different_picture_opens_its_own_case(make_blocked: Callable[..., Page]) -> None:
    first_page = make_blocked("hash-far-first")
    hashed(first_page, bytes(range(32)))
    incidents.open_or_link(first_page, retained=block(first_page))

    other = make_blocked("hash-far-other")
    hashed(other, near(bytes(range(32)), incidents.HASH_MAX_DISTANCE + 1))

    decision = incidents.open_or_link(other, retained=block(other))

    assert decision.action == "created"
    assert SafetyIncident.objects.count() == 2


def test_a_settled_case_is_not_reopened_by_a_matching_hash(
    make_blocked: Callable[..., Page],
) -> None:
    """A human's decision stands: a settled case neither absorbs a new page nor reopens."""
    first_page = make_blocked("hash-settled")
    hashed(first_page, bytes(range(32)))
    first = incidents.open_or_link(first_page, retained=block(first_page))
    assert first.incident is not None
    incidents.mark_false_positive(first.incident, by="tester")

    mirror = make_blocked("hash-settled-mirror")
    hashed(mirror, near(bytes(range(32)), 2))

    decision = incidents.open_or_link(mirror, retained=block(mirror))

    assert decision.action == "created"
    assert decision.incident_id != first.incident_id


def test_a_blocked_page_without_a_stored_hash_still_opens_one_case(
    make_blocked: Callable[..., Page],
) -> None:
    """Nothing is ever fetched to get a hash: a page blocked on first sight simply has none."""
    page = make_blocked("hash-none")

    retained = block(page)
    decision = incidents.open_or_link(page, retained=retained)

    assert (retained, decision.action) == ([], "created")
    assert SafetyIncidentHash.objects.count() == 0

    again = incidents.open_or_link(page, retained=block(page))
    assert (again.action, again.evidence) == ("linked", Evidence.SAME_URL)


def test_keeping_hashes_is_idempotent_and_capped(make_blocked: Callable[..., Page]) -> None:
    page = make_blocked("hash-cap")
    media = Media.objects.create(
        page=page, kind=Media.Kind.PREVIEW, url="https://cdn.example.test/clip.mp4"
    )
    for index in range(incidents.MAX_RETAINED_HASHES + 5):
        FrameHash.objects.create(
            media=media, frame_index=index, pdq=near(bytes(range(32)), index), quality=50 + index
        )

    retained = incidents.collect_page_hashes([page.pk])[page.pk]
    assert len(retained) == incidents.MAX_RETAINED_HASHES
    decision = incidents.open_or_link(page, retained=retained)
    assert decision.incident is not None

    assert incidents.retain_hashes(decision.incident, retained) == 0
    assert SafetyIncidentHash.objects.count() == incidents.MAX_RETAINED_HASHES


def test_retention_deletes_the_hashes_a_case_kept(make_blocked: Callable[..., Page]) -> None:
    now = timezone.now()
    page = make_blocked("hash-retention")
    hashed(page, bytes(range(32)))
    decision = incidents.open_or_link(page, retained=block(page), now=now)
    assert decision.incident is not None
    incidents.mark_false_positive(decision.incident, by="tester", now=now - timedelta(days=400))
    assert SafetyIncidentHash.objects.count() == 1

    assert incidents.apply_retention(now=now).deleted == 1

    assert SafetyIncident.objects.count() == 0
    assert SafetyIncidentHash.objects.count() == 0


def test_pdq_distance_counts_flipped_bits() -> None:
    pdq = bytes(range(32))
    assert incidents.pdq_distance(pdq, pdq) == 0
    assert incidents.pdq_distance(pdq, near(pdq, 5)) == 5
