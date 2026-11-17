"""`lookup_hosting`: which hosts are looked up, in which order, and that a dry run asks nobody.

The lookup itself is `catalog.hosting` (tested there against recorded RDAP fixtures); here it is
replaced by a fake, and the real one is booby-trapped so a test that reaches it fails loudly.
"""

from collections.abc import Callable
from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.utils import timezone

from catalog.hosting.dns import Origin
from catalog.hosting.lookup import HostFacts
from catalog.models import HostingRecord, Site
from index.models import Media, Page
from marceline import hosting
from marceline.hosting import HostLookup, candidate_hostnames, lookup_hosting
from marceline.tests.app_helpers import make_organisation, make_reference
from matching.models import Reference, ReferenceLeak, ReferenceMatch


def facts(hostname: str) -> HostFacts:
    return HostFacts(
        hostname=hostname,
        addresses=("192.0.2.1",),
        address="192.0.2.1",
        origin=Origin(64500, "192.0.2.0/24", "NL", "ripencc", "EXAMPLE-AS"),
        network=None,
        domain=None,
        registrar=None,
        cdn=None,
        errors=(),
    )


@pytest.fixture(autouse=True)
def no_registry_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> tuple[HostLookup, Callable[[], None]]:
        raise AssertionError("a test reached the real DNS/RDAP lookup")

    monkeypatch.setattr(hosting, "registry_lookup", refuse)


@pytest.fixture
def fake_lookup() -> tuple[list[str], Callable[[], tuple[HostLookup, Callable[[], None]]]]:
    asked: list[str] = []

    def lookup(hostname: str) -> HostFacts:
        asked.append(hostname)
        return facts(hostname)

    return asked, lambda: (lookup, lambda: None)


@pytest.fixture
def owner(db: None) -> User:
    return User.objects.create_user("customer-owner")


@pytest.fixture
def original(owner: User) -> Reference:
    # Customer hosts are those of originals an organisation owns (ADR 0031), not of any upload.
    return make_reference(owner, organisation=make_organisation("Customer Studio"))


@pytest.fixture
def world(
    site: Site,
    original: Reference,
    make_page: Callable[[str], Page],
    make_media: Callable[..., Media],
) -> None:
    """A customer finding on a site with a CDN, a flagged page with its own CDN, other sites."""
    Site.objects.create(domain="another-site.test")
    Site.objects.create(domain="disabled-site.test", enabled=False)
    finding = make_page("customer-finding")
    ReferenceLeak.objects.create(reference=original, page=finding, status="confirmed")
    make_media(finding, "https://thumbs.cdn-one.test/a.jpg")
    make_media(finding, "https://thumbs.cdn-one.test/b.jpg", Media.Kind.FRAME)
    make_media(finding, "https://example.test/customer-finding#browser-capture", Media.Kind.CAPTURE)

    flagged = make_page("flagged")
    flagged.safety_status = Page.SafetyStatus.BLOCKED
    flagged.save(update_fields=["safety_status"])
    ReferenceLeak.objects.create(reference=original, page=flagged, status="confirmed")
    make_media(flagged, "https://secret.cdn-two.test/x.jpg")

    # Media of a page no customer has found is not a candidate either.
    make_media(make_page("unmatched"), "https://unrelated.cdn-three.test/y.jpg")


@pytest.mark.usefixtures("world")
def test_customer_hosts_come_first_and_flagged_pages_contribute_nothing() -> None:
    assert candidate_hostnames() == [
        "example.test",
        "thumbs.cdn-one.test",
        "another-site.test",
    ]


@pytest.mark.usefixtures("world")
def test_only_confirmed_findings_rank_their_hosts_first(
    original: Reference, make_media: Callable[..., Media]
) -> None:
    """Hosts of findings nobody confirmed are not customer hosts: a customer does not see them."""
    other = Site.objects.create(domain="proposed-site.test")
    proposed = Page.objects.create(site=other, url="https://proposed-site.test/a", safety_score=1.0)
    ReferenceLeak.objects.create(reference=original, page=proposed, status="proposed")
    make_media(proposed, "https://thumbs.proposed-cdn.test/a.jpg")
    matched = Page.objects.create(site=other, url="https://proposed-site.test/b", safety_score=1.0)
    ReferenceMatch.objects.create(
        reference=original,
        page=matched,
        best_distance=8,
        matched_frames=4,
        page_frames=6,
        best_reference_timestamp_s=12.0,
        evidence_frames=3,
        strength="strong",
        band="high",
    )
    make_media(matched, "https://thumbs.matched-cdn.test/b.jpg")

    assert candidate_hostnames() == [
        "example.test",
        "thumbs.cdn-one.test",
        "another-site.test",
        "proposed-site.test",
    ]


@pytest.mark.usefixtures("world")
def test_a_lookup_that_crashes_is_recorded_as_failed_and_the_run_goes_on() -> None:
    """One poisoned host (an RDAP answer nobody anticipated) must not end every daily run."""
    asked: list[str] = []

    def lookup(hostname: str) -> HostFacts:
        asked.append(hostname)
        if hostname == "example.test":
            raise TypeError("'int' object is not iterable")
        return facts(hostname)

    report = lookup_hosting(lookup_factory=lambda: (lookup, lambda: None))

    assert asked == ["example.test", "thumbs.cdn-one.test", "another-site.test"]
    assert report.results == [
        ("example.test", "failed"),
        ("thumbs.cdn-one.test", "complete"),
        ("another-site.test", "complete"),
    ]
    poisoned = HostingRecord.objects.get(hostname="example.test")
    assert (poisoned.status, poisoned.errors) == ("failed", ["lookup_error"])


@pytest.mark.usefixtures("world")
def test_a_failing_host_backs_off_and_goes_to_the_back_of_the_queue() -> None:
    def lookup(hostname: str) -> HostFacts:
        if hostname == "example.test":
            raise TypeError("poisoned")
        return facts(hostname)

    factory = lambda: (lookup, lambda: None)  # noqa: E731
    start = timezone.now()
    HostingRecord.objects.create(
        hostname="thumbs.cdn-one.test",
        status="complete",
        looked_up_at=start - timedelta(days=60),
    )
    assert lookup_hosting(limit=1, lookup_factory=factory, now=start).due == ["example.test"]

    # A day later the failure is due again, but a stale record and a new host come before it.
    HostingRecord.objects.filter(hostname="example.test").update(
        created_at=start, looked_up_at=start
    )
    HostingRecord.objects.filter(hostname="thumbs.cdn-one.test").update(
        looked_up_at=start - timedelta(days=60)
    )
    later = start + timedelta(days=1)
    report = lookup_hosting(dry_run=True, now=later)
    assert report.due == ["thumbs.cdn-one.test", "another-site.test", "example.test"]

    # After failing for four days in a row it waits four more days, not one.
    HostingRecord.objects.filter(hostname="example.test").update(
        looked_up_at=start + timedelta(days=4)
    )
    assert "example.test" not in lookup_hosting(dry_run=True, now=start + timedelta(days=5)).due
    assert "example.test" in lookup_hosting(dry_run=True, now=start + timedelta(days=8)).due


@pytest.mark.usefixtures("world")
def test_dry_run_lists_what_is_due_and_asks_nobody() -> None:
    out = StringIO()

    call_command("lookup_hosting", dry_run=True, stdout=out)

    assert out.getvalue().splitlines() == [
        "3 candidate host(s), 3 due (dry run, no requests, nothing written)",
        "  due: example.test",
        "  due: thumbs.cdn-one.test",
        "  due: another-site.test",
    ]
    assert HostingRecord.objects.count() == 0
    assert "http" not in out.getvalue()


@pytest.mark.usefixtures("world")
def test_a_run_stores_records_and_honours_limit_and_freshness(
    fake_lookup: tuple[list[str], Callable[[], tuple[HostLookup, Callable[[], None]]]],
) -> None:
    asked, factory = fake_lookup

    report = lookup_hosting(limit=2, lookup_factory=factory)

    assert asked == ["example.test", "thumbs.cdn-one.test"]
    assert report.results == [("example.test", "complete"), ("thumbs.cdn-one.test", "complete")]
    assert set(HostingRecord.objects.values_list("hostname", flat=True)) == set(asked)

    lookup_hosting(lookup_factory=factory)
    assert asked[2:] == ["another-site.test"], "fresh records are not looked up again"

    lookup_hosting(lookup_factory=factory, now=timezone.now() + timedelta(days=31))
    assert len(asked) == 6, "a month later everything is due again"

    lookup_hosting(domain="THUMBS.cdn-one.test", refresh=True, lookup_factory=factory)
    assert asked[-1] == "thumbs.cdn-one.test"


@pytest.mark.usefixtures("world")
def test_nothing_due_opens_no_client(
    fake_lookup: tuple[list[str], Callable[[], tuple[HostLookup, Callable[[], None]]]],
) -> None:
    _, factory = fake_lookup
    lookup_hosting(lookup_factory=factory)

    # Everything is fresh now: the booby-trapped real lookup must not even be constructed.
    call_command("lookup_hosting", stdout=StringIO())


@pytest.mark.usefixtures("world")
@pytest.mark.parametrize("domain", ["secret.cdn-two.test", "disabled-site.test", "anything.test"])
def test_only_candidate_hosts_can_be_looked_up(domain: str) -> None:
    with pytest.raises(CommandError, match="neither an enabled site"):
        call_command("lookup_hosting", domain=domain, stdout=StringIO())
    assert HostingRecord.objects.count() == 0


def test_limit_must_be_positive(db: None) -> None:
    with pytest.raises(CommandError):
        call_command("lookup_hosting", limit=0, stdout=StringIO())
