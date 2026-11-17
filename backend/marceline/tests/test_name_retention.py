"""Retention for a catalogue name and for prospect research (ADR 0048, issue #104).

Invented names throughout: the name is what this feature exists to delete. Every keep rule gets a
test, and so does every guard that must refuse the deletion — including the two the database
enforces rather than Python.
"""

from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from catalog.models import Creator, CreatorSuppression, Site, WatchlistEntry
from crawler.models import CandidateCreator, SourceCandidate
from index.models import Page, ProspectSampleCheck, SafetyRuleDecision, SiteCreatorBreadcrumb
from marceline.models import CreatorRetentionClock
from marceline.name_retention import (
    CREATOR_NAME_RETENTION_DAYS,
    KEEP_ACTIVE_PAGE,
    KEEP_CUSTOMER_ORIGINAL,
    KEEP_PROTECTED,
    KEEP_SAFETY_RECORD,
    KEEP_SOURCE_LIST,
    KEEP_SUPPRESSION,
    KEEP_TARGET,
    KEEP_WATCHLIST,
    RetentionRefused,
    apply_name_retention,
    apply_prospect_retention,
    keep_reasons,
    source_list_names,
)
from marceline.tests.app_helpers import add_member, make_organisation, make_reference
from matching.creators import set_creators

pytestmark = pytest.mark.django_db

#: A `sources.md` with no creators on it: the default for tests that do not exercise that rule.
EMPTY_SOURCES = "Sites:\n- https://example.com/\n\nCreators:\n"


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    path = tmp_path / "sources.md"
    path.write_text(EMPTY_SOURCES)
    return path


def make_creator(name: str) -> Creator:
    return Creator.objects.create(name=name)


def make_page(site: Site, url: str, *, active: bool = True) -> Page:
    return Page.objects.create(
        site=site,
        url=url,
        title="",
        active=active,
        inactive_reason="" if active else "untargeted",
        deactivated_at=None if active else timezone.now(),
    )


def make_breadcrumb(site: Site, creator: Creator) -> SiteCreatorBreadcrumb:
    now = timezone.now()
    return SiteCreatorBreadcrumb.objects.create(
        site=site, creator=creator, page_count=1, first_seen=now, last_seen=now
    )


def aged_clock(creator: Creator, days: int) -> CreatorRetentionClock:
    return CreatorRetentionClock.objects.create(
        creator=creator, unused_since=timezone.now() - timedelta(days=days)
    )


# --- the rule ---------------------------------------------------------------------------------


def test_a_name_nothing_uses_has_no_keep_reason(sources: Path) -> None:
    creator = make_creator("Nora Vale")
    assert keep_reasons([creator.pk], source_names=source_list_names(sources)) == {}


def test_a_suppressed_name_is_kept_even_after_the_suppression_was_lifted(sources: Path) -> None:
    creator = make_creator("Nora Vale")
    CreatorSuppression.objects.create(
        creator=creator,
        requested_on=timezone.now().date(),
        answer_due_on=timezone.now().date(),
        lifted_at=timezone.now(),
        lift_reason="the requester withdrew it",
    )
    reasons = keep_reasons([creator.pk], source_names=source_list_names(sources))
    assert reasons == {creator.pk: KEEP_SUPPRESSION}


def test_a_cast_member_of_an_original_is_customer_data(sources: Path) -> None:
    organisation = make_organisation("Studio")
    owner = add_member(organisation, "studio-owner")
    reference = make_reference(owner, organisation=organisation)
    co_performer = make_creator("Dana Quill")
    set_creators(reference, [reference.creator, co_performer])

    reasons = keep_reasons(
        [reference.creator_id, co_performer.pk], source_names=source_list_names(sources)
    )

    assert reasons[reference.creator_id] == KEEP_CUSTOMER_ORIGINAL
    assert reasons[co_performer.pk] == KEEP_CUSTOMER_ORIGINAL


def test_an_unassigned_original_keeps_its_cast_too(sources: Path) -> None:
    """Ours, but not research: an operator import nobody owns is still not this pass's to expire."""
    reference = make_reference(None)
    assert keep_reasons([reference.creator_id], source_names=source_list_names(sources)) == {
        reference.creator_id: KEEP_CUSTOMER_ORIGINAL
    }


def test_a_watchlist_entry_keeps_a_name(sources: Path) -> None:
    creator = make_creator("Nora Vale")
    WatchlistEntry.objects.create(creator=creator, purpose=WatchlistEntry.Purpose.INTEREST)
    assert keep_reasons([creator.pk], source_names=source_list_names(sources)) == {
        creator.pk: KEEP_WATCHLIST
    }


def test_a_crawled_watchlist_entry_is_a_target(sources: Path) -> None:
    creator = make_creator("Nora Vale")
    WatchlistEntry.objects.create(
        creator=creator, purpose=WatchlistEntry.Purpose.INTEREST, crawl=True
    )
    assert keep_reasons([creator.pk], source_names=source_list_names(sources)) == {
        creator.pk: KEEP_TARGET
    }


def test_an_active_page_link_keeps_a_name_and_a_deactivated_one_does_not(
    site: Site, sources: Path
) -> None:
    linked = make_creator("Nora Vale")
    orphaned = make_creator("Dana Quill")
    make_page(site, "https://example.com/a").creators.add(linked)
    make_page(site, "https://example.com/b", active=False).creators.add(orphaned)

    reasons = keep_reasons([linked.pk, orphaned.pk], source_names=source_list_names(sources))

    assert reasons == {linked.pk: KEEP_ACTIVE_PAGE}


def test_a_safety_record_naming_the_creator_keeps_it(site: Site, sources: Path) -> None:
    queued = make_creator("Nora Vale")
    decided = make_creator("Dana Quill")
    page = make_page(site, "https://example.com/a")
    Page.objects.filter(pk=page.pk).update(safety_release_creator=queued)
    SafetyRuleDecision.objects.create(
        page=page, creator=decided, kind=SafetyRuleDecision.Kind.QUEUED
    )

    reasons = keep_reasons([queued.pk, decided.pk], source_names=source_list_names(sources))

    assert reasons == {queued.pk: KEEP_SAFETY_RECORD, decided.pk: KEEP_SAFETY_RECORD}


def test_a_name_on_the_source_list_is_kept_because_seeding_would_re_create_it(
    tmp_path: Path,
) -> None:
    creator = make_creator("Nora Vale")
    path = tmp_path / "sources.md"
    path.write_text("Sites:\n- https://example.com/\n\nCreators:\n- Nora Vale\n")
    assert keep_reasons([creator.pk], source_names=source_list_names(path)) == {
        creator.pk: KEEP_SOURCE_LIST
    }


def test_needs_review_is_not_a_keep_rule(sources: Path) -> None:
    creator = Creator.objects.create(name="Nora Vale", needs_review=True)
    assert keep_reasons([creator.pk], source_names=source_list_names(sources)) == {}


def test_an_unreadable_source_list_refuses_the_pass(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere.md"
    with pytest.raises(RetentionRefused):
        source_list_names(missing)
    with pytest.raises(RetentionRefused):
        apply_name_retention(dry_run=True, sources=missing)


# --- the clock --------------------------------------------------------------------------------


def test_the_first_run_starts_a_clock_and_deletes_nothing(sources: Path) -> None:
    creator = make_creator("Nora Vale")

    report = apply_name_retention(dry_run=False, sources=sources)

    assert report.marked == [creator.pk] and report.due_ids == []
    assert Creator.objects.filter(pk=creator.pk).exists()
    assert CreatorRetentionClock.objects.get(creator=creator).unused_since is not None


def test_a_dry_run_starts_no_clock(sources: Path) -> None:
    creator = make_creator("Nora Vale")
    report = apply_name_retention(dry_run=True, sources=sources)
    assert report.marked == [creator.pk]
    assert not CreatorRetentionClock.objects.exists()


def test_a_name_that_is_used_again_loses_its_clock(site: Site, sources: Path) -> None:
    creator = make_creator("Nora Vale")
    aged_clock(creator, days=CREATOR_NAME_RETENTION_DAYS - 1)
    make_page(site, "https://example.com/a").creators.add(creator)

    report = apply_name_retention(dry_run=False, sources=sources)

    assert report.cleared == [creator.pk]
    assert not CreatorRetentionClock.objects.exists()
    assert Creator.objects.filter(pk=creator.pk).exists()


def test_a_name_unused_for_the_whole_period_is_deleted_with_its_links(
    site: Site, sources: Path
) -> None:
    creator = make_creator("Nora Vale")
    aged_clock(creator, days=CREATOR_NAME_RETENTION_DAYS + 1)
    deactivated = make_page(site, "https://example.com/a", active=False)
    deactivated.creators.add(creator)
    make_breadcrumb(site, creator)
    candidate = SourceCandidate.objects.create(domain="pirate.example")
    CandidateCreator.objects.create(candidate=candidate, creator=creator)

    report = apply_name_retention(dry_run=False, sources=sources)

    assert report.due_ids == [creator.pk] and report.deleted_ids == [creator.pk]
    assert report.cascade["page_links"] == 1 and report.cascade["breadcrumbs"] == 1
    assert not Creator.objects.filter(pk=creator.pk).exists()
    assert not SiteCreatorBreadcrumb.objects.exists()
    assert not CandidateCreator.objects.exists()
    assert list(deactivated.creators.all()) == []
    # The page itself is ours and is never touched here: its clock is `page_retention`.
    assert Page.objects.filter(pk=deactivated.pk).exists()


def test_a_clock_that_has_not_run_out_deletes_nothing(sources: Path) -> None:
    creator = make_creator("Nora Vale")
    aged_clock(creator, days=CREATOR_NAME_RETENTION_DAYS - 1)
    report = apply_name_retention(dry_run=False, sources=sources)
    assert report.due_ids == []
    assert Creator.objects.filter(pk=creator.pk).exists()


# --- the guards -------------------------------------------------------------------------------


def test_a_suppression_marker_survives_a_deletion_attempt_at_the_orm_level(sources: Path) -> None:
    """The rule keeps it, and `PROTECT` would keep it even if the rule were wrong."""
    creator = make_creator("Nora Vale")
    CreatorSuppression.objects.create(
        creator=creator, requested_on=timezone.now().date(), answer_due_on=timezone.now().date()
    )
    aged_clock(creator, days=CREATOR_NAME_RETENTION_DAYS + 1)

    report = apply_name_retention(dry_run=False, sources=sources)

    assert report.due_ids == [] and report.kept[creator.pk] == KEEP_SUPPRESSION
    with pytest.raises(ProtectedError):
        creator.delete()
    assert Creator.objects.filter(pk=creator.pk).exists()


def test_the_database_itself_refuses_to_delete_a_suppressed_name() -> None:
    """Not only Django: the foreign key refuses raw SQL too (CLAUDE.md, ADR 0048).

    Django creates its foreign keys `DEFERRABLE INITIALLY DEFERRED`, so the refusal lands when the
    constraints are checked — at commit, or when they are made immediate, as here — not on the
    `DELETE` statement itself. Either way the row cannot be removed while the marker stands.
    """
    creator = make_creator("Nora Vale")
    CreatorSuppression.objects.create(
        creator=creator, requested_on=timezone.now().date(), answer_due_on=timezone.now().date()
    )
    with (
        pytest.raises(IntegrityError),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("DELETE FROM catalog_creator WHERE id = %s", [creator.pk])
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert Creator.objects.filter(pk=creator.pk).exists()
    assert CreatorSuppression.objects.filter(creator_id=creator.pk).exists()


def test_a_protected_row_is_reported_as_kept_rather_than_failing_the_run(sources: Path) -> None:
    """A guard that appears between the rule and the delete must not stop the rest of the pass."""
    protected = make_creator("Nora Vale")
    expired = make_creator("Dana Quill")
    aged_clock(protected, days=CREATOR_NAME_RETENTION_DAYS + 1)
    aged_clock(expired, days=CREATOR_NAME_RETENTION_DAYS + 1)
    # A suppression written after the rules were read: the delete is refused by the database.
    CreatorSuppression.objects.create(
        creator=protected, requested_on=timezone.now().date(), answer_due_on=timezone.now().date()
    )

    report = apply_name_retention(
        dry_run=False,
        sources=sources,
        delete_now=[protected.pk, expired.pk],
    )

    assert report.deleted_ids == [expired.pk]
    assert report.kept[protected.pk] in (KEEP_SUPPRESSION, KEEP_PROTECTED)
    assert Creator.objects.filter(pk=protected.pk).exists()


# --- prospects --------------------------------------------------------------------------------


def make_prospect(name: str, *, age_days: int) -> Creator:
    creator = make_creator(name)
    entry = WatchlistEntry.objects.create(
        creator=creator, purpose=WatchlistEntry.Purpose.PROSPECT, crawl=True
    )
    WatchlistEntry.objects.filter(pk=entry.pk).update(
        added_at=timezone.now() - timedelta(days=age_days)
    )
    return creator


def test_prospect_research_past_its_period_is_forgotten(site: Site, sources: Path) -> None:
    creator = make_prospect("Nora Vale", age_days=120)
    page = make_page(site, "https://example.com/a")
    page.creators.add(creator)
    make_breadcrumb(site, creator)
    ProspectSampleCheck.objects.create(
        creator=creator, page=page, verdict=ProspectSampleCheck.Verdict.CORRECT
    )

    report = apply_prospect_retention(dry_run=False, retain_days=90, sources=sources)

    assert report.forgotten_ids == [creator.pk]
    assert report.sample_checks == 1 and report.page_links == 1 and report.breadcrumbs == 1
    assert not WatchlistEntry.objects.exists()
    assert not ProspectSampleCheck.objects.exists()
    assert not SiteCreatorBreadcrumb.objects.exists()
    assert list(page.creators.all()) == []
    # The page stays: it is ours, and its own clock is `page_retention`.
    assert Page.objects.filter(pk=page.pk).exists()


def test_a_prospect_inside_its_period_is_untouched(sources: Path) -> None:
    creator = make_prospect("Nora Vale", age_days=30)
    report = apply_prospect_retention(dry_run=False, retain_days=90, sources=sources)
    assert report.forgotten_ids == []
    assert WatchlistEntry.objects.filter(creator=creator).exists()
    assert report.due_on[creator.pk]


def test_a_prospect_who_became_a_customer_is_kept(sources: Path) -> None:
    organisation = make_organisation("Studio")
    owner = add_member(organisation, "studio-owner")
    reference = make_reference(owner, organisation=organisation)
    creator = make_prospect("Nora Vale", age_days=120)
    set_creators(reference, [reference.creator, creator])

    report = apply_prospect_retention(dry_run=False, retain_days=90, sources=sources)

    assert report.forgotten_ids == []
    assert report.kept == {creator.pk: KEEP_CUSTOMER_ORIGINAL}
    assert WatchlistEntry.objects.filter(creator=creator).exists()


def test_a_forgotten_prospect_loses_the_name_in_the_same_run(site: Site, sources: Path) -> None:
    creator = make_prospect("Nora Vale", age_days=120)
    make_page(site, "https://example.com/a").creators.add(creator)

    prospects = apply_prospect_retention(dry_run=False, retain_days=90, sources=sources)
    names = apply_name_retention(dry_run=False, sources=sources, delete_now=prospects.forgotten_ids)

    assert names.deleted_ids == [creator.pk]
    assert not Creator.objects.filter(pk=creator.pk).exists()
    assert not CreatorRetentionClock.objects.exists()


def test_a_dry_run_sees_the_same_names_as_the_real_run(site: Site, sources: Path) -> None:
    """The prospect pass removes the very rules the name pass would read, so a dry run has to be
    told what is on its way out — otherwise it reports the opposite of what a real run does."""
    creator = make_prospect("Nora Vale", age_days=120)
    make_page(site, "https://example.com/a").creators.add(creator)

    planned_prospects = apply_prospect_retention(dry_run=True, retain_days=90, sources=sources)
    planned_names = apply_name_retention(
        dry_run=True, sources=sources, delete_now=planned_prospects.forgotten_ids
    )

    assert planned_prospects.forgotten_ids == [creator.pk]
    assert planned_names.due_ids == [creator.pk]
    assert Creator.objects.filter(pk=creator.pk).exists()


def test_a_forgotten_prospect_on_the_source_list_keeps_its_name(site: Site, tmp_path: Path) -> None:
    path = tmp_path / "sources.md"
    path.write_text("Sites:\n- https://example.com/\n\nCreators:\n- Nora Vale\n")
    creator = make_prospect("Nora Vale", age_days=120)
    make_page(site, "https://example.com/a").creators.add(creator)

    prospects = apply_prospect_retention(dry_run=False, retain_days=90, sources=path)
    names = apply_name_retention(dry_run=False, sources=path, delete_now=prospects.forgotten_ids)

    assert prospects.forgotten_ids == [creator.pk]
    assert names.deleted_ids == []
    assert names.kept[creator.pk] == KEEP_SOURCE_LIST
    assert Creator.objects.filter(pk=creator.pk).exists()


# --- the command ------------------------------------------------------------------------------


def run(*args: str, sources: Path) -> str:
    out = StringIO()
    call_command("research_retention", *args, "--sources", str(sources), stdout=out)
    return out.getvalue()


def test_the_command_defaults_to_a_dry_run(site: Site, sources: Path) -> None:
    creator = make_creator("Nora Vale")
    aged_clock(creator, days=CREATOR_NAME_RETENTION_DAYS + 1)

    text = run(sources=sources)

    assert "dry run, nothing written" in text
    assert Creator.objects.filter(pk=creator.pk).exists()


def test_the_command_prints_ids_and_counts_but_never_a_name(site: Site, sources: Path) -> None:
    creator = make_prospect("Nora Vale", age_days=120)
    make_page(site, "https://example.com/a").creators.add(creator)

    text = run(sources=sources)

    assert str(creator.pk) in text
    assert "Nora" not in text


def test_the_command_applies_every_pass(sources: Path) -> None:
    creator = make_creator("Nora Vale")
    aged_clock(creator, days=CREATOR_NAME_RETENTION_DAYS + 1)

    run("--apply", sources=sources)

    assert not Creator.objects.filter(pk=creator.pk).exists()


def test_the_command_refuses_without_a_readable_source_list(tmp_path: Path) -> None:
    with pytest.raises(CommandError):
        run("--apply", sources=tmp_path / "nowhere.md")
