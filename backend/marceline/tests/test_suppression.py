"""Applying and lifting an index suppression, and everything it must keep a name out of.

docs/suppression.md. Invented names throughout: a suppressed person's real name is exactly what
this feature exists to remove.
"""

from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from catalog.do_not_contact import EMAIL
from catalog.models import Creator, CreatorSuppression, Site, WatchlistEntry
from catalog.services import contact_check, record_no_contact
from crawler.discovery.services import plan_queries
from crawler.models import CandidateCreator, CrawlTask, SearchCall, SourceCandidate
from crawler.sites.common import creator_names, name_keys, names_person
from index.breadcrumbs import record_page_links
from index.models import FrameHash, Media, Page, ProspectSampleCheck, SiteCreatorBreadcrumb
from marceline.prospects import prospect_report
from marceline.review.creators import MergeRefused, merge_creator
from marceline.suppression import (
    SuppressionRefused,
    answer_due,
    apply_suppression,
    lift_suppression,
    plan_suppression,
    redact,
)
from marceline.targets import targets
from marceline.tests.app_helpers import add_member, make_organisation, make_reference
from matching.creators import set_creators

pytestmark = pytest.mark.django_db

REQUESTED = date(2026, 9, 17)


@pytest.fixture
def site(db: None) -> Site:
    return Site.objects.create(domain="example.test", crawl_delay_s=0)


@pytest.fixture
def nora(db: None) -> Creator:
    return Creator.objects.create(name="Nora Vale", aliases=["Nora V"])


@pytest.fixture
def dana(db: None) -> Creator:
    return Creator.objects.create(name="Dana Quill")


def make_page(site: Site, slug: str, **fields: object) -> Page:
    return Page.objects.create(site=site, url=f"https://example.test/{slug}", **fields)


# --- the pure parts ---------------------------------------------------------------------------


def test_names_person_matches_whole_words_and_separator_free_spellings() -> None:
    keys = name_keys(["Nora Vale", "Nora V"])

    assert names_person("Nora Vale and friends", keys)
    assert names_person("nora_vale", keys)
    assert names_person("NoraVale", keys)
    assert not names_person("Noralie Valentine", keys)
    assert not names_person("Dana Quill", keys)
    assert not names_person("", keys)


def test_redact_removes_only_the_parts_that_name_the_person() -> None:
    keys = name_keys(["Nora Vale"])
    raw = {
        "performers": ["Nora Vale", "Dana Quill"],
        "profile_slug": "nora-vale",
        "duration_s": 612,
        "nested": {"handle": "NoraVale", "studio": "Some Studio"},
    }

    cleaned, dropped = redact(raw, keys)

    assert cleaned == {
        "performers": ["Dana Quill"],
        "duration_s": 612,
        "nested": {"studio": "Some Studio"},
    }
    assert sorted(dropped) == ["nested.handle", "performers", "profile_slug"]


def test_answer_due_is_one_month_and_clamps_a_short_month() -> None:
    assert answer_due(date(2026, 9, 17)) == date(2026, 10, 17)
    assert answer_due(date(2026, 12, 31)) == date(2027, 1, 31)
    assert answer_due(date(2026, 1, 31)) == date(2026, 2, 28)


# --- applying ---------------------------------------------------------------------------------


def test_apply_drops_the_association_and_keeps_the_rest(
    site: Site, nora: Creator, dana: Creator
) -> None:
    shared = make_page(
        site,
        "shared",
        title="Nora Vale and Dana Quill video",
        tags=["nora vale", "hd"],
        raw_extraction={"performers": ["Nora Vale", "Dana Quill"], "duration_s": 600},
    )
    shared.creators.add(nora, dana)
    record_page_links(shared, [nora.pk, dana.pk])
    ruled_out = make_page(
        site,
        "compilation",
        title="Best of",
        raw_extraction={"performers": ["Nora Vale"], "compilation": {"reasons": ["many"]}},
    )
    WatchlistEntry.objects.create(creator=nora, purpose=WatchlistEntry.Purpose.PROSPECT)
    ProspectSampleCheck.objects.create(
        creator=nora, page=shared, verdict=ProspectSampleCheck.Verdict.CORRECT
    )
    candidate = SourceCandidate.objects.create(
        domain="pirate.example",
        distinct_creators=2,
        evidence={"queries": [{"query": "Nora Vale leaked"}, {"query": "Dana Quill leaked"}]},
    )
    CandidateCreator.objects.create(candidate=candidate, creator=nora)
    CandidateCreator.objects.create(candidate=candidate, creator=dana)
    SearchCall.objects.create(query="Nora Vale leaked", mode="standard", status="ok")
    SearchCall.objects.create(query="Dana Quill leaked", mode="standard", status="ok")

    plan, suppression = apply_suppression(
        nora,
        requested_on=REQUESTED,
        identity_check=CreatorSuppression.IdentityCheck.PLATFORM_MESSAGE,
        decided_by="operator",
    )

    # Gone: every statement that this person is on these pages.
    assert plan.page_links == 1 and plan.breadcrumbs == 1
    assert list(shared.creators.all()) == [dana]
    assert list(SiteCreatorBreadcrumb.objects.values_list("creator_id", flat=True)) == [dana.pk]
    assert not ProspectSampleCheck.objects.exists()
    assert not WatchlistEntry.objects.exists()
    assert list(CandidateCreator.objects.values_list("creator_id", flat=True)) == [dana.pk]
    candidate.refresh_from_db()
    assert candidate.evidence["queries"] == [{"query": "Dana Quill leaked"}]
    assert candidate.distinct_creators == 1
    # Scrubbed, not deleted (ADR 0048): the query text naming them is gone, the row stays, and so
    # does its cost — these rows are ADR 0013's spend ledger.
    assert SearchCall.objects.count() == 2
    assert sorted(SearchCall.objects.values_list("query", flat=True)) == ["", "Dana Quill leaked"]
    scrubbed = SearchCall.objects.get(query="")
    assert scrubbed.query_scrubbed_at is not None
    assert SearchCall.objects.exclude(query="").get().query_scrubbed_at is None

    # Gone: the name-bearing stored extraction, on a page that never linked them too.
    shared.refresh_from_db()
    ruled_out.refresh_from_db()
    assert shared.tags == ["hd"]
    assert shared.raw_extraction == {"performers": ["Dana Quill"], "duration_s": 600}
    # An emptied list stays an empty list: that is what a page crediting nobody looks like.
    assert ruled_out.raw_extraction == {"performers": [], "compilation": {"reasons": ["many"]}}

    # Kept: the pages, their URLs and their titles; the other performer's link.
    assert Page.objects.count() == 2
    assert shared.title == "Nora Vale and Dana Quill video"
    assert shared.url == "https://example.test/shared"

    # The record.
    assert suppression.active and suppression.applied_at is not None
    assert suppression.requested_on == REQUESTED
    assert suppression.answer_due_on == date(2026, 10, 17)
    assert suppression.removed_page_links == 1
    assert suppression.removed_breadcrumbs == 1
    assert suppression.redacted_pages == 2
    assert suppression.decided_by == "operator"


def test_applying_twice_changes_nothing_further(site: Site, nora: Creator) -> None:
    page = make_page(site, "a", raw_extraction={"performers": ["Nora Vale"]})
    page.creators.add(nora)

    apply_suppression(nora, requested_on=REQUESTED)
    _, again = apply_suppression(nora, requested_on=date(2026, 9, 30))

    assert again.requested_on == REQUESTED  # the first request is the record
    assert again.removed_page_links == 1 and again.redacted_pages == 1
    plan = plan_suppression(nora)
    assert plan.changes == 0


def test_apply_refuses_a_rights_holder_of_an_owned_original(nora: Creator) -> None:
    organisation = make_organisation("A Studio")
    owner = add_member(organisation, "studio-owner")
    reference = make_reference(owner, organisation=organisation)
    reference.creator = nora
    reference.save(update_fields=["creator"])
    set_creators(reference, [nora])

    with pytest.raises(SuppressionRefused, match="rights holder"):
        apply_suppression(nora, requested_on=REQUESTED)

    assert not CreatorSuppression.objects.exists()


def test_a_co_performer_of_an_owned_original_is_reported_but_never_edited(
    nora: Creator, dana: Creator
) -> None:
    organisation = make_organisation("A Studio")
    owner = add_member(organisation, "studio-owner")
    reference = make_reference(owner, organisation=organisation)
    reference.creator = dana
    reference.save(update_fields=["creator"])
    set_creators(reference, [dana, nora])

    plan, _ = apply_suppression(nora, requested_on=REQUESTED)

    assert (plan.owned_as_rights_holder, plan.owned_as_co_performer) == (0, 1)
    assert set(reference.creators.all()) == {dana, nora}  # customer contract data, untouched


# --- safety-flagged pages stay off-limits -----------------------------------------------------

FLAGGED_TAGS = ["nora vale", "hd"]
FLAGGED_RAW = {"performers": ["Nora Vale"], "duration_s": 300}
MATCHES = [{"term": "a term", "category": "minor", "action": "auto_block_and_incident"}]


def test_a_flagged_page_keeps_its_text_while_the_association_still_goes(
    site: Site, nora: Creator
) -> None:
    """Operator rule: flagged is off-limits whoever is asking — a request is no exception."""
    flagged_at = timezone.now()
    blocked = make_page(
        site,
        "blocked",
        title="Nora Vale video",
        tags=list(FLAGGED_TAGS),
        raw_extraction=dict(FLAGGED_RAW),
        safety_status=Page.SafetyStatus.BLOCKED,
        safety_matches=list(MATCHES),
        safety_flagged_at=flagged_at,
        safety_score=0.0,
    )
    review = make_page(
        site,
        "review",
        tags=list(FLAGGED_TAGS),
        raw_extraction=dict(FLAGGED_RAW),
        safety_status=Page.SafetyStatus.REVIEW,
        safety_matches=list(MATCHES),
    )
    review.creators.add(nora)  # a review page can carry a link; a blocked one never does
    record_page_links(review, [nora.pk])
    clear = make_page(site, "clear", tags=list(FLAGGED_TAGS), raw_extraction=dict(FLAGGED_RAW))

    plan, suppression = apply_suppression(nora, requested_on=REQUESTED)

    assert plan.flagged_pages == 2
    assert plan.redacted_pages == 1
    for page in (blocked, review):
        page.refresh_from_db()
        assert page.tags == FLAGGED_TAGS
        assert page.raw_extraction == FLAGGED_RAW
        assert page.safety_matches == MATCHES
        assert page.safety_status in (Page.SafetyStatus.BLOCKED, Page.SafetyStatus.REVIEW)
    blocked.refresh_from_db()
    assert blocked.safety_flagged_at == flagged_at and blocked.safety_score == 0.0
    # The association goes everywhere, though: it is not part of the safety record.
    assert review.creators.count() == 0
    assert not SiteCreatorBreadcrumb.objects.exists()
    clear.refresh_from_db()
    assert clear.tags == ["hd"] and clear.raw_extraction == {"performers": [], "duration_s": 300}
    assert suppression.redacted_pages == 1


def test_a_lift_never_re_opens_a_flagged_page(site: Site, nora: Creator) -> None:
    blocked = make_page(
        site,
        "blocked",
        tags=list(FLAGGED_TAGS),
        raw_extraction=dict(FLAGGED_RAW),
        safety_status=Page.SafetyStatus.BLOCKED,
        safety_matches=list(MATCHES),
        safety_score=0.0,
    )
    apply_suppression(nora, requested_on=REQUESTED)

    lift_suppression(nora, reason="withdrawn by the person")

    blocked.refresh_from_db()
    assert blocked.safety_status == Page.SafetyStatus.BLOCKED
    assert blocked.safety_matches == MATCHES and blocked.safety_score == 0.0
    assert blocked.tags == FLAGGED_TAGS and blocked.raw_extraction == FLAGGED_RAW
    assert blocked.creators.count() == 0  # nothing is linked back
    # Nothing is fetched, queued or made fingerprintable by either step.
    assert not Media.objects.exists()
    assert not CrawlTask.objects.exists()
    assert not FrameHash.objects.exists()


def test_the_command_reports_the_flagged_pages_it_leaves_alone(site: Site, nora: Creator) -> None:
    make_page(
        site,
        "blocked",
        tags=list(FLAGGED_TAGS),
        safety_status=Page.SafetyStatus.BLOCKED,
        safety_matches=list(MATCHES),
    )
    out = StringIO()

    call_command("suppress_creator", "--creator", nora.name, stdout=out)

    assert "safety-flagged:    1 page(s) name them and are left untouched" in out.getvalue()


# --- what a suppressed name is kept out of ----------------------------------------------------


def test_a_suppressed_name_is_never_a_target(nora: Creator, dana: Creator) -> None:
    organisation = make_organisation("A Studio")
    owner = add_member(organisation, "studio-owner")
    reference = make_reference(owner, organisation=organisation)
    reference.creator = dana
    reference.save(update_fields=["creator"])
    set_creators(reference, [dana, nora])
    WatchlistEntry.objects.create(creator=nora, crawl=True)
    assert {target.creator for target in targets()} == {dana, nora}

    apply_suppression(nora, requested_on=REQUESTED)

    assert {target.creator for target in targets()} == {dana}


def test_no_discovery_query_may_name_a_suppressed_person(nora: Creator, dana: Creator) -> None:
    apply_suppression(nora, requested_on=REQUESTED)

    planned = plan_queries([], [])
    assert planned
    assert not any("Nora" in query.text for query in planned)

    with pytest.raises(ValueError, match="Suppressed"):
        plan_queries(["Nora Vale"], [])
    with pytest.raises(ValueError, match="suppressed person"):
        plan_queries([], ["nora_vale free video"])


def test_a_suppressed_name_has_no_prospect_report(site: Site, nora: Creator) -> None:
    WatchlistEntry.objects.create(creator=nora, purpose=WatchlistEntry.Purpose.PROSPECT)
    assert prospect_report(nora).creator == nora.name

    apply_suppression(nora, requested_on=REQUESTED)

    with pytest.raises(ValueError, match="suppressed"):
        prospect_report(nora)
    with pytest.raises(CommandError, match="suppressed"):
        call_command("prospect_report", "--creator", nora.name, stdout=StringIO())


def test_crawl_seed_refuses_a_suppressed_name_and_a_query_naming_them(
    site: Site, nora: Creator
) -> None:
    apply_suppression(nora, requested_on=REQUESTED)

    with pytest.raises(CommandError, match="suppressed"):
        call_command("crawl_seed", "--creator", nora.name, stdout=StringIO())
    with pytest.raises(CommandError, match="suppressed"):
        call_command("crawl_seed", "--query", "Nora Vale video", stdout=StringIO())


# --- lifting ----------------------------------------------------------------------------------


def test_lift_restores_nothing_but_ends_the_exclusion(site: Site, nora: Creator) -> None:
    page = make_page(site, "a", raw_extraction={"performers": ["Nora Vale"]})
    page.creators.add(nora)
    apply_suppression(nora, requested_on=REQUESTED)

    suppression = lift_suppression(nora, reason="withdrawn by the person", decided_by="operator")

    assert suppression.lifted_at is not None
    assert suppression.lift_reason == "withdrawn by the person"
    assert list(Creator.objects.usable()) == [nora]
    page.refresh_from_db()
    assert page.creators.count() == 0  # a re-crawl re-links; nothing is put back here
    assert page.raw_extraction == {"performers": []}


def test_lift_is_idempotent_and_needs_a_reason(nora: Creator) -> None:
    apply_suppression(nora, requested_on=REQUESTED)
    first = lift_suppression(nora, reason="withdrawn")
    again = lift_suppression(nora, reason="withdrawn twice")

    assert again.lifted_at == first.lifted_at
    assert again.lift_reason == "withdrawn"

    other = Creator.objects.create(name="Dana Quill")
    with pytest.raises(SuppressionRefused, match="no suppression"):
        lift_suppression(other, reason="none")
    with pytest.raises(SuppressionRefused, match="--reason"):
        lift_suppression(nora, reason="  ")


def test_suppressing_again_after_a_lift_records_the_new_request(nora: Creator) -> None:
    apply_suppression(nora, requested_on=REQUESTED)
    lift_suppression(nora, reason="withdrawn")

    _, suppression = apply_suppression(
        nora,
        basis=CreatorSuppression.Basis.ERASURE,
        requested_on=date(2026, 11, 2),
        identity_check=CreatorSuppression.IdentityCheck.COUNSEL,
    )

    assert suppression.active and suppression.lift_reason == ""
    assert suppression.requested_on == date(2026, 11, 2)
    assert suppression.basis == CreatorSuppression.Basis.ERASURE


# --- the command ------------------------------------------------------------------------------


def test_the_command_dry_runs_first_then_applies(site: Site, nora: Creator) -> None:
    page = make_page(site, "a", tags=["nora vale"], raw_extraction={"performers": ["Nora Vale"]})
    page.creators.add(nora)
    record_page_links(page, [nora.pk])

    out = StringIO()
    call_command("suppress_creator", "--creator", nora.name, stdout=out)
    dry = out.getvalue()

    assert "page links:        1 would be removed" in dry
    assert "dry run" in dry.lower()
    assert not CreatorSuppression.objects.exists()
    assert page.creators.count() == 1

    out = StringIO()
    call_command(
        "suppress_creator",
        "--creator",
        nora.name,
        "--apply",
        "--requested-on",
        "2026-09-17",
        "--identity-check",
        "studio_or_agency",
        stdout=out,
    )
    applied = out.getvalue()

    assert "Applied." in applied and "answer due 2026-10-17" in applied
    assert page.creators.count() == 0
    assert not SiteCreatorBreadcrumb.objects.exists()

    listed = StringIO()
    call_command("suppress_creator", "--list", stdout=listed)
    assert "Nora Vale" in listed.getvalue()


def test_the_command_lifts_only_with_apply_and_a_reason(nora: Creator) -> None:
    apply_suppression(nora, requested_on=REQUESTED)

    out = StringIO()
    call_command(
        "suppress_creator", "--creator", nora.name, "--lift", "--reason", "withdrawn", stdout=out
    )
    assert "Dry run" in out.getvalue()
    assert CreatorSuppression.objects.get().active

    with pytest.raises(CommandError, match="--reason"):
        call_command("suppress_creator", "--creator", nora.name, "--lift", "--apply")

    call_command(
        "suppress_creator",
        "--creator",
        nora.name,
        "--lift",
        "--apply",
        "--reason",
        "withdrawn",
        stdout=StringIO(),
    )
    assert not CreatorSuppression.objects.get().active


def test_the_command_refuses_an_unknown_name() -> None:
    with pytest.raises(CommandError, match="unknown creator"):
        call_command("suppress_creator", "--creator", "Nobody Here", stdout=StringIO())


def test_suppression_survives_a_creator_merge_attempt(nora: Creator) -> None:
    """A merge must not move a suppressed name onto another creator or into its aliases."""
    typed = Creator.objects.create(name="Nora  Vale", needs_review=True)
    apply_suppression(nora, requested_on=REQUESTED)

    with pytest.raises(MergeRefused, match="Suppressed"):
        merge_creator(typed, nora, by=None, as_alias=True)

    apply_suppression(typed, requested_on=REQUESTED)
    with pytest.raises(MergeRefused, match="Suppressed"):
        merge_creator(typed, Creator.objects.create(name="Dana Quill"), by=None, as_alias=True)


def test_a_do_not_contact_record_survives_the_suppression_of_its_creator(nora: Creator) -> None:
    """The two markers are separate promises (issue #98, docs/do-not-contact.md).

    Suppression drops what says "this person is on these sites" — including the prospect trail.
    The objection to being contacted is not part of that trail: it is the reason we may never
    write again, so it outlives the suppression, the lift and the creator row itself.
    """
    key = "test-key-not-a-real-one"
    row, _ = record_no_contact(
        kind=EMAIL,
        identifier="post@example.org",
        received_on=REQUESTED,
        channel="letter",
        recorded_by="operator",
        creator=nora,
        secret=key,
    )

    apply_suppression(nora, requested_on=REQUESTED)
    assert contact_check(EMAIL, ["post@example.org"], key) == [True]

    lift_suppression(nora, reason="withdrawn by the person")
    assert contact_check(EMAIL, ["post@example.org"], key) == [True]

    row.refresh_from_db()
    assert row.creator_id == nora.pk


def test_the_aliases_a_suppression_covers_are_reported(nora: Creator) -> None:
    plan = plan_suppression(nora)

    assert plan.names == creator_names(nora)
    assert "Nora V" in plan.names
