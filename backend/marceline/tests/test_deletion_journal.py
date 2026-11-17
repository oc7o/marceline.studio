"""The deletion journal and its replay (ADR 0057, DPIA R24, issues #137 / #140 D3).

Three things are tested, and they are the three the feature is worth nothing without:

1. **every irreversible privacy path appends its line** — the hook, at the single writer of each;
2. **the line never carries the deleted content**: the format refuses anything but ids, hex digests,
   ISO dates and short enum tokens, and a real suppression run's file is searched for the name, the
   alias, the page title and the URL;
3. **a replay against an older database restores the deleted state**, idempotently, and says what it
   could not do instead of doing something else.

Invented names throughout: a name is what several of these paths exist to delete.
"""

import json
from collections.abc import Callable
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from pytest_django.fixtures import DjangoCaptureOnCommitCallbacks, Settings

from catalog.models import (
    Creator,
    CreatorSuppression,
    DoNotContactRecord,
    Site,
    WatchlistEntry,
)
from catalog.services import contact_check, record_no_contact
from core import deletion_journal
from crawler.models import SearchCall, SourceCandidate
from index import incidents as safety_incidents
from index import retention as page_retention
from index.models import Page, SafetyIncident, SiteCreatorBreadcrumb
from marceline import deletion_replay, name_retention, suppression
from marceline.deletion.models import OrganisationEnding, OriginalRemoval
from marceline.deletion.retention import delete_organisation, delete_original
from marceline.tests.app_helpers import add_member, make_organisation, make_reference

pytestmark = pytest.mark.django_db

Capture = DjangoCaptureOnCommitCallbacks

#: The invented performer this file suppresses, and a page that credits them.
NAME = "Quillon Sarabask"
ALIAS = "quillonsara"
TITLE = "Quillon Sarabask backstage clip 4"
URL = "https://example.test/video/quillon-sarabask-4/"
ADDRESS = "Beispielstrasse 4, 10117 Berlin"


@pytest.fixture
def journal(settings: Settings, tmp_path: Path) -> Path:
    """A journal directory of this test's own, never the operator's."""
    directory = tmp_path / "journal"
    settings.DELETION_JOURNAL_DIR = str(directory)
    settings.DELETION_JOURNAL_SEGMENT = "deletions"
    return directory


@pytest.fixture
def lines(journal: Path) -> Callable[[], list[dict[str, Any]]]:
    """Every journal line written so far, parsed."""

    def _read() -> list[dict[str, Any]]:
        return [
            json.loads(raw)
            for path in deletion_journal.segment_files(journal)
            for raw in path.read_text().splitlines()
            if raw.strip()
        ]

    return _read


def kinds(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["kind"]) for row in rows]


@pytest.fixture
def creator(db: None) -> Creator:
    return Creator.objects.create(name=NAME, aliases=[ALIAS])


@pytest.fixture
def credited_page(site: Site, creator: Creator) -> Page:
    """A safety-clear page that names the performer in its title, tags and extraction."""
    page = Page.objects.create(
        site=site,
        url=URL,
        title=TITLE,
        tags=[NAME, "backstage"],
        raw_extraction={"performers": [NAME], "handle": ALIAS, "duration_s": 61},
        safety_score=1.0,
    )
    page.creators.set([creator])
    SiteCreatorBreadcrumb.objects.create(
        site=site,
        creator=creator,
        page_count=1,
        first_seen=timezone.now(),
        last_seen=timezone.now(),
    )
    return page


# --- 1. Every deletion path writes its line ---------------------------------------------------


def test_a_suppression_writes_one_line_with_no_name_in_it(
    journal: Path,
    lines: Callable[[], list[dict[str, Any]]],
    creator: Creator,
    credited_page: Page,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        suppression.apply_suppression(
            creator,
            basis=CreatorSuppression.Basis.OBJECTION,
            identity_check=CreatorSuppression.IdentityCheck.PLATFORM_MESSAGE,
            note=f"the person writing from {ALIAS}",
            decided_by="operator",
        )
    rows = lines()
    assert kinds(rows) == ["creator_suppression"]
    assert rows[0]["creator"] == creator.pk
    assert rows[0]["basis"] == "objection"
    assert rows[0]["identity"] == "platform_message"
    # The whole point: the file that survives the restore holds no name and no title.
    written = "\n".join(path.read_text() for path in deletion_journal.segment_files(journal))
    for secret in (NAME, ALIAS, TITLE, URL, "operator"):
        assert secret not in written


def test_a_lift_is_journalled_as_its_own_kind(
    lines: Callable[[], list[dict[str, Any]]],
    creator: Creator,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        suppression.apply_suppression(creator)
        suppression.lift_suppression(creator, reason="withdrawn by the person")
    assert kinds(lines()) == ["creator_suppression", "creator_suppression_lifted"]


def test_a_page_text_scrub_journals_the_pages_it_actually_clears(
    lines: Callable[[], list[dict[str, Any]]],
    credited_page: Page,
    make_page: Callable[[str], Page],
    django_capture_on_commit_callbacks: Capture,
) -> None:
    empty = make_page("nothing-to-clear")
    Page.objects.filter(pk=empty.pk).update(title="", tags=[], raw_extraction={})
    with django_capture_on_commit_callbacks(execute=True):
        page_retention.scrub_deactivated_pages([credited_page.pk, empty.pk])
    rows = lines()
    # One line, for the one page that still held something: the journal is what has to be
    # re-applied, not what was passed in.
    assert rows == [{**rows[0], "kind": "page_text_scrub", "page": credited_page.pk}]
    assert kinds(rows) == ["page_text_scrub"]


def test_a_flagged_page_is_never_journalled_because_it_is_never_scrubbed(
    lines: Callable[[], list[dict[str, Any]]],
    credited_page: Page,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    Page.objects.filter(pk=credited_page.pk).update(safety_status=Page.SafetyStatus.BLOCKED)
    with django_capture_on_commit_callbacks(execute=True):
        page_retention.scrub_deactivated_pages([credited_page.pk])
    assert lines() == []


def test_page_retention_journals_the_page_and_its_url_digest(
    lines: Callable[[], list[dict[str, Any]]],
    credited_page: Page,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    _deactivate(credited_page, months=30)
    with django_capture_on_commit_callbacks(execute=True):
        report = page_retention.apply_retention(dry_run=False)
    assert report.deleted_ids == [credited_page.pk]
    rows = lines()
    assert kinds(rows) == ["page_text_scrub", "page_deleted"] or kinds(rows) == ["page_deleted"]
    deleted = next(row for row in rows if row["kind"] == "page_deleted")
    assert deleted["page"] == credited_page.pk
    assert len(str(deleted["url_sha256"])) == 64
    assert URL not in json.dumps(rows)


def test_a_dry_run_journals_nothing(
    lines: Callable[[], list[dict[str, Any]]],
    credited_page: Page,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    _deactivate(credited_page, months=30)
    with django_capture_on_commit_callbacks(execute=True):
        page_retention.apply_retention(dry_run=True)
    assert lines() == []


def test_safety_incident_retention_journals_the_incident_id(
    lines: Callable[[], list[dict[str, Any]]],
    db: None,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    incident = _settled_incident(months=24)
    with django_capture_on_commit_callbacks(execute=True):
        report = safety_incidents.apply_retention(dry_run=False)
    assert report.deleted_ids == [incident.pk]
    assert lines() == [{**lines()[0], "kind": "safety_incident_deleted", "incident": incident.pk}]


def test_the_query_scrub_journals_the_call_and_the_candidate(
    lines: Callable[[], list[dict[str, Any]]],
    db: None,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    from crawler import retention as query_retention

    old = timezone.now() - timedelta(days=90)
    call = SearchCall.objects.create(engine="serp", query=NAME, cost_usd=0)
    SearchCall.objects.filter(pk=call.pk).update(created_at=old)
    candidate = SourceCandidate.objects.create(
        domain="example.test", evidence={"queries": [{"query": NAME, "rank": 1}]}
    )
    SourceCandidate.objects.filter(pk=candidate.pk).update(last_seen_at=old)
    with django_capture_on_commit_callbacks(execute=True):
        query_retention.apply_query_retention(dry_run=False)
    rows = lines()
    assert sorted(kinds(rows)) == ["candidate_queries_scrubbed", "search_query_scrubbed"]
    assert NAME not in json.dumps(rows)


def test_prospect_and_name_retention_journal_the_creator_id(
    lines: Callable[[], list[dict[str, Any]]],
    creator: Creator,
    django_capture_on_commit_callbacks: Capture,
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources.md"
    sources.write_text("# no creators here\n")
    entry = WatchlistEntry.objects.create(creator=creator, purpose=WatchlistEntry.Purpose.PROSPECT)
    WatchlistEntry.objects.filter(pk=entry.pk).update(added_at=timezone.now() - timedelta(days=200))
    with django_capture_on_commit_callbacks(execute=True):
        forgotten = name_retention.apply_prospect_retention(dry_run=False, sources=sources)
        name_retention.apply_name_retention(
            dry_run=False, sources=sources, delete_now=forgotten.forgotten_ids
        )
    assert kinds(lines()) == ["prospect_research_forgotten", "creator_name_deleted"]
    assert not Creator.objects.filter(pk=creator.pk).exists()


def test_a_do_not_contact_record_is_journalled_when_it_is_written(
    settings: Settings,
    lines: Callable[[], list[dict[str, Any]]],
    db: None,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = "test-key"
    with django_capture_on_commit_callbacks(execute=True):
        record, created = record_no_contact(
            kind="postal",
            identifier=ADDRESS,
            received_on=timezone.now().date(),
            channel="letter",
            recorded_by="operator",
            note="objection to the letter",
        )
    assert created
    rows = lines()
    assert kinds(rows) == ["do_not_contact_recorded"]
    assert rows[0]["identifier_hash"] == record.identifier_hash
    assert rows[0]["identifier_kind"] == "postal"
    assert rows[0]["channel"] == "letter"
    written = json.dumps(rows)
    for secret in (ADDRESS, "Beispielstrasse", "Berlin", "operator", "objection to the letter"):
        assert secret not in written


def test_recording_the_same_no_twice_journals_once(
    settings: Settings,
    lines: Callable[[], list[dict[str, Any]]],
    db: None,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = "test-key"
    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(2):
            record_no_contact(
                kind="email",
                identifier="post@example.org",
                received_on=timezone.now().date(),
                channel="email",
                recorded_by="operator",
            )
    assert kinds(lines()) == ["do_not_contact_recorded"]


def test_customer_data_deletion_journals_both_scopes(
    lines: Callable[[], list[dict[str, Any]]],
    db: None,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    organisation = make_organisation("Pilot Studio")
    user = add_member(organisation, "pilot-owner")
    reference = make_reference(user, organisation=organisation)
    removal = OriginalRemoval.objects.create(
        organisation=organisation,
        sha256=reference.sha256,
        removed_at=timezone.now() - timedelta(days=60),
        reason=OriginalRemoval.Reason.OWNER_CHANGED,
    )
    ending = OrganisationEnding.objects.create(
        organisation=organisation, ended_at=timezone.now() - timedelta(days=60)
    )
    now = timezone.now()
    with django_capture_on_commit_callbacks(execute=True):
        delete_original(removal, now=now, dry_run=False)
        delete_organisation(ending, now=now, dry_run=False)
    rows = lines()
    assert "original_data_deleted" in kinds(rows)
    assert "organisation_data_deleted" in kinds(rows)
    original = next(row for row in rows if row["kind"] == "original_data_deleted")
    assert original["organisation"] == organisation.pk
    assert original["sha256"] == reference.sha256
    assert "Pilot Studio" not in json.dumps(rows)

    # And a replay right afterwards is quiet: both scopes are in effect, and the organisation row
    # took its `OrganisationEnding` mark with it — which must read as `already`, not as "a human
    # must re-mark this pilot", or the weekly restore check would complain about it for ever.
    report = deletion_replay.replay(dry_run=False)
    assert (report.reapplied, report.failed, report.needs_human) == (0, 0, 0)
    assert report.already == len(rows)


def test_a_customer_scope_with_no_mark_but_data_still_there_needs_a_human(
    journal: Path, db: None, django_capture_on_commit_callbacks: Capture
) -> None:
    """ADR 0045 runs off a date a human set; a replay never invents one."""
    organisation = make_organisation("Marked Studio")
    user = add_member(organisation, "marked-owner")
    reference = make_reference(user, organisation=organisation)
    removal = OriginalRemoval.objects.create(
        organisation=organisation,
        sha256=reference.sha256,
        removed_at=timezone.now() - timedelta(days=60),
        reason=OriginalRemoval.Reason.OWNER_CHANGED,
    )
    with django_capture_on_commit_callbacks(execute=True):
        deletion_journal.record(
            "original_data_deleted", organisation=organisation.pk, sha256=reference.sha256
        )
        deletion_journal.record("organisation_data_deleted", organisation=organisation.pk)
    removal.delete()  # the restore predates the mark, but the file is still owned

    report = deletion_replay.replay(dry_run=True)

    assert report.needs_human == 2
    assert report.failed == 0


# --- 2. The format cannot carry content -------------------------------------------------------


def test_the_format_refuses_an_undeclared_field() -> None:
    with pytest.raises(deletion_journal.JournalRefused):
        deletion_journal.record("creator_name_deleted", creator=1, name=NAME)


def test_the_format_refuses_free_text_in_a_token_field() -> None:
    with pytest.raises(deletion_journal.JournalRefused):
        deletion_journal.record(
            "creator_suppression",
            creator=1,
            basis=NAME,
            requested_on="2026-09-19",
            identity="platform_message",
        )


def test_the_format_refuses_something_that_is_not_a_digest() -> None:
    with pytest.raises(deletion_journal.JournalRefused):
        deletion_journal.record(
            "original_data_deleted", organisation=1, sha256="https://example.test/x"
        )


def test_the_format_refuses_an_unknown_kind() -> None:
    with pytest.raises(deletion_journal.JournalRefused):
        deletion_journal.record("delete_everything", creator=1)


def test_every_field_of_every_kind_has_a_shape_that_cannot_hold_a_sentence() -> None:
    """A new kind must not introduce a free-text field. This is the guard for that."""
    allowed = {deletion_journal.INT, deletion_journal.HEX64, deletion_journal.DATE}
    for kind, fields in deletion_journal.KIND_FIELDS.items():
        for name, shape in fields.items():
            assert shape in allowed | {deletion_journal.TOKEN}, f"{kind}.{name}"
            if shape == deletion_journal.TOKEN:
                # A token is at most 32 lower-case characters with no space: an enum value, never
                # a name, a title, an address or a note.
                assert deletion_journal._TOKEN.pattern.endswith("{0,31}$")


def test_a_deletion_with_no_journal_configured_still_happens(
    settings: Settings, creator: Creator, credited_page: Page
) -> None:
    """Honouring an objection never depends on a writable file."""
    settings.DELETION_JOURNAL_DIR = ""
    suppression.apply_suppression(creator)
    assert not Page.objects.filter(pk=credited_page.pk, creators=creator).exists()


# --- 3. The replay ----------------------------------------------------------------------------


def test_a_restored_database_is_brought_back_to_the_suppressed_state(
    journal: Path,
    creator: Creator,
    credited_page: Page,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    """The worst case of R24: the snapshot predates the request, so even the marker is gone."""
    with django_capture_on_commit_callbacks(execute=True):
        suppression.apply_suppression(
            creator,
            basis=CreatorSuppression.Basis.ERASURE,
            identity_check=CreatorSuppression.IdentityCheck.COUNSEL,
        )
    # The restore: the marker goes, and everything it removed comes back.
    CreatorSuppression.objects.filter(creator=creator).delete()
    Page.objects.filter(pk=credited_page.pk).update(
        title=TITLE, tags=[NAME], raw_extraction={"performers": [NAME]}
    )
    credited_page.creators.set([creator])
    SiteCreatorBreadcrumb.objects.create(
        site=credited_page.site,
        creator=creator,
        page_count=1,
        first_seen=timezone.now(),
        last_seen=timezone.now(),
    )
    WatchlistEntry.objects.create(creator=creator)
    assert Page.objects.filter(pk=credited_page.pk, creators=creator).exists()

    report = deletion_replay.replay(dry_run=False)

    assert report.failed == 0
    assert report.reapplied == 1
    marker = CreatorSuppression.objects.get(creator=creator)
    assert marker.lifted_at is None
    assert marker.basis == CreatorSuppression.Basis.ERASURE
    assert marker.identity_check == CreatorSuppression.IdentityCheck.COUNSEL
    assert not Page.objects.filter(pk=credited_page.pk, creators=creator).exists()
    assert not SiteCreatorBreadcrumb.objects.filter(creator=creator).exists()
    assert not WatchlistEntry.objects.filter(creator=creator).exists()
    page = Page.objects.get(pk=credited_page.pk)
    assert page.tags == []
    assert page.raw_extraction == {"performers": []}  # the entry goes, the key stays


def test_the_replay_is_idempotent(
    journal: Path,
    creator: Creator,
    credited_page: Page,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        suppression.apply_suppression(creator)
    first = deletion_replay.replay(dry_run=False)
    second = deletion_replay.replay(dry_run=False)
    third = deletion_replay.replay(dry_run=True)
    assert first.reapplied + first.already == 1
    assert (second.already, second.reapplied, second.failed) == (1, 0, 0)
    assert (third.already, third.reapplied, third.failed) == (1, 0, 0)


def test_a_lost_do_not_contact_record_is_re_inserted(
    settings: Settings,
    journal: Path,
    db: None,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    settings.DO_NOT_CONTACT_HMAC_KEY = "test-key"
    received = timezone.now().date() - timedelta(days=3)
    with django_capture_on_commit_callbacks(execute=True):
        record_no_contact(
            kind="postal",
            identifier=ADDRESS,
            received_on=received,
            channel="letter",
            recorded_by="operator",
            note="do not write again",
        )
    # The restore: the append-only record simply loses its newest rows.
    DoNotContactRecord.objects.all().delete()
    assert contact_check("postal", [ADDRESS], "test-key") == [False]

    report = deletion_replay.replay(dry_run=False)

    assert report.reapplied == 1
    assert contact_check("postal", [ADDRESS], "test-key") == [True]
    restored = DoNotContactRecord.objects.get()
    assert restored.identifier_kind == "postal"
    assert restored.received_on == received
    assert restored.channel == "letter"
    assert restored.recorded_by == deletion_replay.REPLAY_ACTOR
    assert "deletion journal" in restored.note


def test_a_resurrected_page_is_deleted_again(
    journal: Path, credited_page: Page, django_capture_on_commit_callbacks: Capture
) -> None:
    _deactivate(credited_page, months=30)
    page_id, url, site = credited_page.pk, credited_page.url, credited_page.site
    with django_capture_on_commit_callbacks(execute=True):
        page_retention.apply_retention(dry_run=False)
    assert not Page.objects.filter(pk=page_id).exists()
    Page.objects.create(pk=page_id, site=site, url=url, title=TITLE)

    report = deletion_replay.replay(dry_run=False)

    assert report.failed == 0
    assert not Page.objects.filter(pk=page_id).exists()


def test_a_different_url_at_the_same_id_is_refused_rather_than_deleted(
    journal: Path, credited_page: Page, django_capture_on_commit_callbacks: Capture
) -> None:
    _deactivate(credited_page, months=30)
    page_id, site = credited_page.pk, credited_page.site
    with django_capture_on_commit_callbacks(execute=True):
        page_retention.apply_retention(dry_run=False)
    Page.objects.create(pk=page_id, site=site, url="https://example.test/somebody-else/")

    report = deletion_replay.replay(dry_run=True)

    assert report.failed == 1
    assert Page.objects.filter(pk=page_id).exists()


def test_an_open_case_again_is_listed_for_a_human_not_deleted(
    journal: Path, db: None, django_capture_on_commit_callbacks: Capture
) -> None:
    incident = _settled_incident(months=24)
    with django_capture_on_commit_callbacks(execute=True):
        safety_incidents.apply_retention(dry_run=False)
    revived = SafetyIncident.objects.create(
        pk=incident.pk,
        status=SafetyIncident.Status.OPEN,
        category="minor",
        term="t",
        detected_at=timezone.now(),
        last_seen_at=timezone.now(),
    )

    report = deletion_replay.replay(dry_run=False)

    assert report.needs_human == 1
    assert report.failed == 0
    assert SafetyIncident.objects.filter(pk=revived.pk).exists()


def test_a_lift_is_reported_for_a_human_and_never_applied(
    journal: Path, creator: Creator, django_capture_on_commit_callbacks: Capture
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        suppression.apply_suppression(creator)
        suppression.lift_suppression(creator, reason="withdrawn")
    CreatorSuppression.objects.filter(creator=creator).delete()

    report = deletion_replay.replay(dry_run=False)

    assert report.needs_human == 1
    # The suppression came back and the lift did not: the safe direction.
    assert CreatorSuppression.objects.get(creator=creator).lifted_at is None


def test_a_suppression_whose_name_is_not_in_the_restored_database_is_reported(
    journal: Path, creator: Creator, django_capture_on_commit_callbacks: Capture
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        suppression.apply_suppression(creator)
    CreatorSuppression.objects.filter(creator=creator).delete()
    Creator.objects.filter(pk=creator.pk).delete()

    report = deletion_replay.replay(dry_run=True)

    assert report.failed == 1
    outcome = next(row for row in report.outcomes if row.kind == "creator_suppression")
    assert f"creator={creator.pk}" in outcome.failed[0][0]


def test_an_unreadable_line_is_reported_never_skipped(journal: Path, db: None) -> None:
    journal.mkdir(parents=True, exist_ok=True)
    (journal / "deletions.jsonl").write_text('{"kind": "creator_name_deleted"\n')
    report = deletion_replay.replay(dry_run=True)
    assert report.unreadable and report.entries == 0


def test_since_skips_entries_older_than_the_dump(
    journal: Path, creator: Creator, django_capture_on_commit_callbacks: Capture
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        suppression.apply_suppression(creator)
    later = timezone.now() + timedelta(minutes=5)
    assert deletion_replay.replay(dry_run=True, since=later).entries == 0
    assert deletion_replay.replay(dry_run=True).entries == 1


# --- The command ------------------------------------------------------------------------------


def test_the_command_refuses_when_no_journal_is_configured(settings: Settings, db: None) -> None:
    settings.DELETION_JOURNAL_DIR = ""
    with pytest.raises(CommandError, match="DELETION_JOURNAL_DIR"):
        call_command("reapply_deletions", stdout=StringIO())


def test_the_command_is_a_dry_run_by_default_and_prints_a_summary(
    journal: Path,
    creator: Creator,
    credited_page: Page,
    django_capture_on_commit_callbacks: Capture,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        suppression.apply_suppression(creator)
    CreatorSuppression.objects.filter(creator=creator).delete()
    out = StringIO()
    call_command("reapply_deletions", stdout=out)
    printed = out.getvalue()
    assert "dry run, nothing written" in printed
    assert "reapply-deletions entries=1" in printed
    assert "failed=0" in printed
    assert not CreatorSuppression.objects.filter(creator=creator).exists()

    out = StringIO()
    call_command("reapply_deletions", "--apply", "--quiet", stdout=out)
    assert out.getvalue().strip().startswith("reapply-deletions entries=1")
    assert CreatorSuppression.objects.filter(creator=creator).exists()


def test_the_command_fails_when_an_entry_could_not_be_applied(
    journal: Path, creator: Creator, django_capture_on_commit_callbacks: Capture
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        suppression.apply_suppression(creator)
    CreatorSuppression.objects.filter(creator=creator).delete()
    Creator.objects.filter(pk=creator.pk).delete()
    with pytest.raises(CommandError, match="could not be applied"):
        call_command("reapply_deletions", "--apply", stdout=StringIO())


def test_every_journal_kind_has_a_replay_handler() -> None:
    assert set(deletion_replay.HANDLERS) == set(deletion_journal.KIND_FIELDS)
    assert set(deletion_replay.REPLAY_ORDER) == set(deletion_journal.KIND_FIELDS)


# --- helpers ----------------------------------------------------------------------------------


def _deactivate(page: Page, *, months: int) -> None:
    """Put a page where `page_retention` will delete it: deactivated as untargeted, long ago."""
    Page.objects.filter(pk=page.pk).update(
        active=False,
        inactive_reason=Page.InactiveReason.UNTARGETED,
        deactivated_at=timezone.now() - timedelta(days=31 * months),
    )


def _settled_incident(*, months: int) -> SafetyIncident:
    return SafetyIncident.objects.create(
        status=SafetyIncident.Status.CLOSED,
        category="minor",
        term="t",
        detected_at=timezone.now() - timedelta(days=31 * (months + 1)),
        last_seen_at=timezone.now() - timedelta(days=31 * (months + 1)),
        closed_at=timezone.now() - timedelta(days=31 * months),
    )
