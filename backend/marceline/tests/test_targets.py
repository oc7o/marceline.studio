"""Targeted indexing (issue #71): targets, the active-page rule, `deactivate_untargeted`."""

from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import Membership
from catalog.models import Creator, Site, WatchlistEntry
from crawler.models import CrawlTask
from crawler.sites.base import SEED_PRIORITY, WATCHLIST_SEED_PRIORITY
from index.models import FrameHash, Media, Page, SiteCreatorBreadcrumb
from marceline.targets import (
    CO_PERFORMER_MAX_PRIORITY,
    CO_PERFORMER_MIN_PRIORITY,
    NoTargetsError,
    TargetKind,
    apply_activity,
    co_performer_priority,
    plan_activity,
    targets,
    uncrawled_watchlist,
)
from marceline.tests.app_helpers import add_member, make_organisation, own
from matching.creators import set_creators
from matching.models import Cluster, ClusterMember, Reference, ReferenceLeak, ReferenceMatch

pytestmark = pytest.mark.django_db

UNTARGETED = Page.InactiveReason.UNTARGETED
MATCH = {
    "best_distance": 8,
    "matched_frames": 4,
    "page_frames": 6,
    "best_reference_timestamp_s": 12.0,
}


def original(
    primary: Creator, *others: Creator, owner: User | None = None, title: str = "o"
) -> Reference:
    reference = Reference.objects.create(
        creator=primary, title=title, sha256=title.ljust(64, "0"), frame_fps=1, uploaded_by=owner
    )
    set_creators(reference, [primary, *others])
    membership = Membership.objects.filter(user=owner).first() if owner is not None else None
    if membership is not None:
        own(reference, membership.organisation)
    return reference


def page(site: Site, slug: str, *creators: Creator, **fields: object) -> Page:
    created = Page.objects.create(
        site=site, url=f"https://{site.domain}/{slug}", title=slug, **fields
    )
    created.creators.add(*creators)
    return created


def names(kind: TargetKind) -> list[str]:
    return [target.creator.name for target in targets() if target.kind == kind]


@pytest.mark.parametrize(
    ("originals", "partners", "priority"),
    [
        (1, 1, 12),  # a one-off co-actor: the floor
        (1, 0, 12),
        (2, 1, 15),
        (4, 1, 21),
        (4, 3, 23),
        (4, 9, 24),  # the partner bonus is capped
        (20, 5, 28),  # never at or above a rights holder
    ],
)
def test_co_performer_priority(originals: int, partners: int, priority: int) -> None:
    assert co_performer_priority(originals, partners) == priority


def test_co_performer_priority_stays_in_its_band() -> None:
    values = [co_performer_priority(o, p) for o in range(40) for p in range(40)]
    assert min(values) == CO_PERFORMER_MIN_PRIORITY > WATCHLIST_SEED_PRIORITY
    assert max(values) == CO_PERFORMER_MAX_PRIORITY < SEED_PRIORITY


def test_targets_rank_rights_holders_then_co_performers_by_prominence() -> None:
    studio, one_off, regular, unowned, watched, crawled, both = (
        Creator.objects.create(name=name)
        for name in (
            "Studio Star",
            "One Off",
            "Regular",
            "Operator Import",
            "Watched",
            "Crawled Watch",
            "Both",
        )
    )
    owner = add_member(make_organisation("Studio"), "studio")
    original(studio, one_off, regular, owner=owner, title="owned-1")
    original(studio, regular, owner=owner, title="owned-2")
    original(both, regular, owner=owner, title="owned-3")
    original(unowned, title="operator-import")  # unassigned: owns nothing
    WatchlistEntry.objects.create(creator=watched, note="prospect")  # noted, not crawled
    WatchlistEntry.objects.create(creator=crawled, crawl=True)
    WatchlistEntry.objects.create(creator=both)

    hunted = [(t.creator.name, t.kind, t.priority, t.originals, t.partners) for t in targets()]

    assert hunted == [
        ("Both", TargetKind.RIGHTS_HOLDER, SEED_PRIORITY, 1, 1),
        ("Studio Star", TargetKind.RIGHTS_HOLDER, SEED_PRIORITY, 2, 2),
        # three originals, alongside Studio Star, One Off and Both
        ("Regular", TargetKind.CO_PERFORMER, co_performer_priority(3, 3), 3, 3),
        ("One Off", TargetKind.CO_PERFORMER, co_performer_priority(1, 2), 1, 2),
        ("Crawled Watch", TargetKind.WATCHLIST, WATCHLIST_SEED_PRIORITY, 0, 0),
    ]
    assert [creator.name for creator in uncrawled_watchlist()] == ["Watched"]
    # A user without a membership owns nothing either.
    loner = User.objects.create_user("loner")
    original(unowned, owner=loner, title="loner")
    assert "Operator Import" not in names(TargetKind.RIGHTS_HOLDER)


@pytest.fixture
def world() -> dict[str, Page]:
    """One target (a rights holder) and one page per rule, plus pages of nobody we hunt."""
    site = Site.objects.create(domain="site01.example")
    target = Creator.objects.create(name="Lena Hartwell")
    other = Creator.objects.create(name="Someone Else")
    owner = add_member(make_organisation("Studio"), "studio")
    target_original = original(target, owner=owner, title="target")
    other_original = original(other, title="other")

    pages = {
        "linked": page(site, "linked", target, other),
        "matched": page(site, "matched"),
        "leaked": page(site, "leaked"),
        "clustered": page(site, "clustered", other),
        "seeded": page(site, "video/1/seeded"),
        "blocked": page(site, "blocked", safety_status=Page.SafetyStatus.BLOCKED),
        "other_linked": page(site, "other-linked", other),
        "other_matched": page(site, "other-matched"),
        "related": page(site, "video/2/related"),
        "rejected_member": page(site, "rejected-member"),
    }
    ReferenceMatch.objects.create(reference=target_original, page=pages["matched"], **MATCH)
    ReferenceLeak.objects.create(reference=target_original, page=pages["leaked"], score=1.0)
    ReferenceMatch.objects.create(reference=other_original, page=pages["other_matched"], **MATCH)
    cluster = Cluster.objects.create()
    ClusterMember.objects.create(cluster=cluster, page=pages["linked"], status="confirmed")
    ClusterMember.objects.create(cluster=cluster, page=pages["clustered"])
    ClusterMember.objects.create(cluster=cluster, page=pages["rejected_member"], status="rejected")
    # The target's search seed -> its page 2 -> a detail; that detail's related video is not ours.
    seed = CrawlTask.objects.create(
        site=site, url="https://site01.example/search/lena-hartwell/", kind=CrawlTask.Kind.SEARCH
    )
    page_two = CrawlTask.objects.create(
        site=site,
        url="https://site01.example/search/lena-hartwell/?from=2",
        kind=CrawlTask.Kind.SEARCH,
        discovered_from=seed,
    )
    detail = CrawlTask.objects.create(
        site=site, url=pages["seeded"].url, kind=CrawlTask.Kind.DETAIL, discovered_from=page_two
    )
    CrawlTask.objects.create(
        site=site, url=pages["related"].url, kind=CrawlTask.Kind.DETAIL, discovered_from=detail
    )
    # A cycle must not hang the walk.
    CrawlTask.objects.filter(pk=seed.pk).update(discovered_from=page_two)
    return pages


def active_slugs() -> set[str]:
    return {
        url.rsplit("/", 1)[-1]
        for url in Page.objects.filter(active=True).values_list("url", flat=True)
    }


def test_apply_deactivates_untargeted_pages_and_is_idempotent(world: dict[str, Page]) -> None:
    result = apply_activity()

    assert active_slugs() == {"linked", "matched", "leaked", "clustered", "seeded", "blocked"}
    assert (result.deactivated, result.reactivated) == (4, 0)
    inactive = Page.objects.get(pk=world["other_linked"].pk)
    assert (inactive.inactive_reason, inactive.active_changed_at is not None) == (UNTARGETED, True)
    # Nothing is deleted, and a second run changes nothing.
    assert Page.objects.count() == len(world)
    again = apply_activity()
    assert (again.deactivated, again.reactivated) == (0, 0)
    # Breadcrumbs cover every creator, target or not, among pages still active (ADR 0029
    # amendment 2026-09-17): "other-linked" deactivated, so its link to Someone Else no longer
    # counts, but "linked" and "clustered" (both active) still do.
    assert dict(SiteCreatorBreadcrumb.objects.values_list("creator__name", "page_count")) == {
        "Lena Hartwell": 1,
        "Someone Else": 2,
    }


def test_deactivation_scrubs_people_but_keeps_what_reactivation_needs(
    world: dict[str, Page],
) -> None:
    other_linked = world["other_linked"]
    Page.objects.filter(pk=other_linked.pk).update(
        tags=["outdoor"],
        raw_extraction={
            "performers": ["Someone Else"],
            "dropped_performers": [{"name": "Extra", "reason": "noise_word"}],
            "duration_s": 42,
            "external_links": ["https://mirror.test/x"],
        },
    )
    url, site_id, first_seen = other_linked.url, other_linked.site_id, other_linked.first_seen
    linked = world["linked"]
    linked_tags, linked_raw = ["outdoor"], {"performers": ["Lena Hartwell", "Someone Else"]}
    Page.objects.filter(pk=linked.pk).update(tags=linked_tags, raw_extraction=linked_raw)

    apply_activity()

    scrubbed = Page.objects.get(pk=other_linked.pk)
    assert (scrubbed.title, scrubbed.tags) == ("", [])
    assert scrubbed.raw_extraction == {
        "duration_s": 42,
        "external_links": ["https://mirror.test/x"],
    }
    # Page.creators is kept (the `linked` rule needs it to reactivate the page later); it just
    # stops feeding the breadcrumbs the moment the page deactivates (index.breadcrumbs).
    assert {c.name for c in scrubbed.creators.all()} == {"Someone Else"}
    assert scrubbed.deactivated_at == scrubbed.active_changed_at
    # What a reactivation needs is untouched.
    assert (scrubbed.url, scrubbed.site_id, scrubbed.first_seen) == (url, site_id, first_seen)
    # A target's page (kept active) is never touched.
    kept = Page.objects.get(pk=linked.pk)
    assert (kept.title, kept.tags, kept.raw_extraction) == ("linked", linked_tags, linked_raw)
    assert {c.name for c in kept.creators.all()} == {"Lena Hartwell", "Someone Else"}
    assert kept.deactivated_at is None


def test_a_new_target_reactivates_its_pages(world: dict[str, Page]) -> None:
    apply_activity()
    before = Page.objects.get(pk=world["other_linked"].pk).active_changed_at

    # Noting a creator on the watchlist keeps nothing active; ticking `crawl` does.
    entry = WatchlistEntry.objects.create(creator=Creator.objects.get(name="Someone Else"))
    assert apply_activity().reactivated == 0
    entry.crawl = True
    entry.save()
    result = apply_activity()

    assert result.reactivated == 2  # other-linked, other-matched
    reactivated = Page.objects.get(pk=world["other_linked"].pk)
    assert (reactivated.active, reactivated.inactive_reason) == (True, "")
    assert before is not None and reactivated.active_changed_at is not None
    assert reactivated.active_changed_at > before
    # Reactivation clears the retention clock (ADR 0029 amendment 2026-09-17).
    assert reactivated.deactivated_at is None


def test_plan_counts_per_site_without_writing(world: dict[str, Page]) -> None:
    for name in ("linked", "other_linked"):
        media = Media.objects.create(
            page=world[name], kind="poster", url=f"https://cdn.test/{name}"
        )
        FrameHash.objects.create(media=media, pdq=bytes(32), quality=100)

    plan = plan_activity()

    [site] = plan.sites
    assert (site.domain, site.pages, site.active, site.keep, site.deactivate) == (
        "site01.example",
        10,
        10,
        6,
        4,
    )
    assert (site.frames, site.frames_keep, site.frames_deactivate) == (2, 1, 1)
    assert plan.reasons == {"linked": 1, "matched": 2, "clustered": 2, "seeded": 1, "safety": 1}
    assert not Page.objects.filter(active=False).exists()


def test_no_targets_refuses_to_apply(db: None) -> None:
    Page.objects.create(site=Site.objects.create(domain="a.test"), url="https://a.test/1")

    with pytest.raises(NoTargetsError):
        apply_activity()
    with pytest.raises(CommandError, match="Refusing"):
        call_command("deactivate_untargeted", apply=True, stdout=StringIO())
    assert Page.objects.get().active


def test_command_dry_run_reports_and_apply_changes(world: dict[str, Page]) -> None:
    WatchlistEntry.objects.create(creator=Creator.objects.create(name="Noted Only"))
    out = StringIO()
    call_command("deactivate_untargeted", stdout=out)

    text = out.getvalue()
    assert "Targets (rights_holder): 1: Lena Hartwell" in text
    assert "Targets (watchlist): 0: none" in text
    assert "Watchlist, not crawled (no seeds, pages not kept active): 1: Noted Only" in text
    assert "site01.example" in text and "Dry run: nothing changed." in text
    assert not Page.objects.filter(active=False).exists()

    out = StringIO()
    call_command("deactivate_untargeted", apply=True, stdout=out)
    assert (
        "Applied: 4 page(s) deactivated, 0 reactivated; 2 breadcrumb(s) rebuilt." in out.getvalue()
    )


def test_stop_broad_listings_skips_only_pending_catalogue_chains(world: dict[str, Page]) -> None:
    site = Site.objects.create(domain="site09.example")
    pending = CrawlTask.Status.PENDING

    def task(path: str, kind: str, parent: CrawlTask | None = None, **fields: object) -> CrawlTask:
        return CrawlTask.objects.create(
            site=site, url=f"https://site09.example{path}", kind=kind, discovered_from=parent, **fields
        )

    newest = task("/videos", "listing", status=CrawlTask.Status.DONE)
    page_two = task("/videos?page=2", "listing", newest)
    page_three = task("/videos?page=3", "listing", page_two)
    detail = task("/someone/video/1", "detail", page_two)
    creator_seed = task("/lenahartwell/video", "listing")
    out = StringIO()

    call_command("deactivate_untargeted", stdout=out)
    assert "Pending broad-listing tasks (--stop-broad-listings): 2" in out.getvalue()

    call_command("deactivate_untargeted", apply=True, stop_broad_listings=True, stdout=out)

    assert "Stopped 2 broad-listing task(s)." in out.getvalue()
    statuses = dict(CrawlTask.objects.filter(site=site).values_list("pk", "status"))
    assert statuses[page_two.pk] == statuses[page_three.pk] == CrawlTask.Status.SKIPPED
    assert statuses[detail.pk] == statuses[creator_seed.pk] == pending
