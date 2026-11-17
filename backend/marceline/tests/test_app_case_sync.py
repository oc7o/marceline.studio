"""Reconciling cases when two readers, or a reader and a job, touch the same original at once.

`sync_cases` runs on every customer read (while `CASE_SYNC_ON_READ` is on) and from
`sync_all_cases`. Two of them working on the same original must neither create its case twice
(a 500 from the one-case-per-original constraint) nor write to a case the other just deleted.
The sequential tests below replay exactly those interleavings deterministically, by letting
"another request" run in the middle of a sync; the threaded one runs them for real on Postgres.
"""

import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from io import StringIO
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import override_settings

from accounts.models import Organisation
from catalog.models import Site
from index.models import Page
from marceline.app import cases
from marceline.app.originals import confirmed_findings_of
from marceline.tests.app_helpers import (
    APP,
    add_member,
    client_for,
    grouped,
    make_organisation,
    make_reference,
    public_id_of,
)
from matching.models import CaseFinding, LeakCase, Reference, ReferenceLeak, ReferenceMatch

FIRST_CHECK = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def owner(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


@pytest.fixture
def customer(owner: User) -> Client:
    return client_for(owner)


def confirm(reference: Reference, page: Page) -> ReferenceLeak:
    return ReferenceLeak.objects.create(reference=reference, page=page, status="confirmed")


def take_down(page: Page, at: datetime) -> None:
    page.availability = Page.Availability.REMOVED
    page.availability_checked_at = at
    page.save(update_fields=["availability", "availability_checked_at"])


@pytest.fixture
def meanwhile(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[], object]], None]:
    """Run `action` — another request — once, in the middle of the next sync's first finding."""

    def install(action: Callable[[], object]) -> None:
        original = cases._sync_finding
        fired = False

        def interleaved(*args: Any, **kwargs: Any) -> CaseFinding:
            nonlocal fired
            if not fired:
                fired = True
                action()
            return original(*args, **kwargs)

        monkeypatch.setattr(cases, "_sync_finding", interleaved)

    return install


def test_a_case_created_by_another_request_meanwhile_is_not_created_twice(
    owner: User,
    make_page: Callable[[str], Page],
    meanwhile: Callable[[Callable[[], object]], None],
) -> None:
    first = make_reference(owner, title="first")
    second = make_reference(owner, title="second")
    confirm(first, make_page("one"))
    confirm(second, make_page("two"))
    meanwhile(lambda: cases.sync_cases(grouped([second])))

    views = cases.sync_cases(grouped([first, second]))

    assert LeakCase.objects.filter(reference=second).count() == 1
    assert CaseFinding.objects.filter(case__reference=second).count() == 1
    assert (
        views[1].case is not None and views[1].case.pk == LeakCase.objects.get(reference=second).pk
    )


def test_a_case_deleted_by_another_request_meanwhile_is_not_written_to(
    owner: User,
    make_page: Callable[[str], Page],
    meanwhile: Callable[[Callable[[], object]], None],
) -> None:
    first = make_reference(owner, title="first")
    second = make_reference(owner, title="second")
    confirm(first, make_page("one"))
    page = make_page("two")
    confirm(second, page)
    cases.sync_cases(grouped([second]))
    # Gone at the next check, so this sync has something to stamp on the second case ...
    take_down(page, FIRST_CHECK)

    def flag_and_sync() -> None:
        # ... while another request finds the page flagged and deletes that case.
        page.safety_status = Page.SafetyStatus.BLOCKED
        page.save(update_fields=["safety_status"])
        cases.sync_cases(grouped([second]))

    meanwhile(flag_and_sync)

    views = cases.sync_cases(grouped([first, second]))

    assert (views[1].case, views[1].rows) == (None, [])
    assert not LeakCase.objects.filter(reference=second).exists()
    assert not CaseFinding.objects.filter(page=page).exists()


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_first_reads_open_one_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real connections, real commits: both threads read "no case yet" if nothing serialises them.

    Each thread pauses right after reading the findings until the other has read too (or a second
    has passed). Unserialised, both then create the case and one fails on the constraint; with
    the per-original lock the second thread cannot even start reading until the first committed.
    """
    organisation = make_organisation("Race Studio")
    owner = add_member(organisation, "race-owner")
    reference = make_reference(owner, title="raced")
    site = Site.objects.create(domain="race.test", name="Race")
    site_page = Page.objects.create(
        site=site, url="https://race.test/one", title="one", safety_score=1.0
    )
    confirm(reference, site_page)

    both_read = threading.Barrier(2, timeout=1.0)
    read = confirmed_findings_of

    def slow_read(*args: Any, **kwargs: Any) -> Any:
        rows = read(*args, **kwargs)
        with suppress(threading.BrokenBarrierError):
            both_read.wait()
        return rows

    monkeypatch.setattr(cases, "confirmed_findings_of", slow_read)
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            cases.sync_cases(grouped([Reference.objects.get(pk=reference.pk)]))
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert LeakCase.objects.filter(reference=reference).count() == 1
    assert CaseFinding.objects.filter(case__reference=reference).count() == 1


def test_with_sync_on_read_off_a_read_writes_nothing_and_shows_the_last_sync(
    customer: Client, owner: User, make_page: Callable[[str], Page]
) -> None:
    """ADR 0028: the public cache must not write on read; the sync job keeps cases current."""
    original = make_reference(owner, title="synced by the job")
    page = make_page("confirmed-leak")
    confirm(original, page)

    with override_settings(CASE_SYNC_ON_READ=False):
        assert customer.get(f"{APP}/cases").json() == []
        assert customer.get(f"{APP}/findings").json() == []
        assert customer.get(f"{APP}/cases/{public_id_of(original)}").json()["case"] is None
        assert (LeakCase.objects.count(), CaseFinding.objects.count()) == (0, 0)

        out = StringIO()
        call_command("sync_cases", stdout=out)
        assert out.getvalue().strip() == "1 originals reconciled, 1 with a case"
        [case] = customer.get(f"{APP}/cases").json()
        assert case["original_id"] == public_id_of(original)
        assert [row["page_id"] for row in customer.get(f"{APP}/findings").json()] == [
            page.public_id
        ]

        take_down(page, FIRST_CHECK)
        stamp = CaseFinding.objects.get().updated_at
        assert customer.get(f"{APP}/removals").json() == []  # observed by the next sync only
        assert CaseFinding.objects.get().updated_at == stamp

        # Visibility is never stale: a page flagged since the last sync is gone at once.
        page.safety_status = Page.SafetyStatus.BLOCKED
        page.save(update_fields=["safety_status"])
        detail = customer.get(f"{APP}/cases/{public_id_of(original)}")
        assert customer.get(f"{APP}/cases").json() == []
        assert customer.get(f"{APP}/findings").json() == []
        assert (detail.json()["case"], detail.json()["findings"]) == (None, [])
        assert page.url not in detail.content.decode()


def test_sync_all_cases_covers_every_organisation_and_no_unowned_original(
    owner: User, make_page: Callable[[str], Page]
) -> None:
    other = add_member(make_organisation("Beta Studio"), "beta-owner")
    mine = make_reference(owner, title="alpha")
    theirs = make_reference(other, title="beta")
    unowned = make_reference(None, title="operator import")
    for reference, slug in ((mine, "a"), (theirs, "b"), (unowned, "c")):
        confirm(reference, make_page(slug))

    views = cases.sync_all_cases()

    assert sorted(view.reference.pk for view in views) == sorted([mine.pk, theirs.pk])
    assert set(LeakCase.objects.values_list("reference_id", flat=True)) == {mine.pk, theirs.pk}
    cases.sync_all_cases()
    assert (LeakCase.objects.count(), CaseFinding.objects.count()) == (2, 2)


def test_confirmed_on_does_not_move_when_matching_saves_the_decision_again(
    customer: Client, owner: User, make_page: Callable[[str], Page]
) -> None:
    """L4: `match_references` re-saves every match row, which moves its `updated_at` each run.

    The first sync after a confirmation stores that moment in `CaseFinding.verified_at`, and from
    then on the stored observation wins, whether the read syncs or not. (A decision re-saved
    *before* any sync ran can only contribute the `updated_at` it has by then — the documented
    limit of decision tables that carry no decision date.)
    """
    decided = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    original = make_reference(owner, title="auto-matched")
    decision = ReferenceMatch.objects.create(
        reference=original,
        page=make_page("matched"),
        best_distance=8,
        matched_frames=4,
        page_frames=6,
        best_reference_timestamp_s=12.0,
        evidence_frames=3,
        strength="strong",
        band="high",
        status="confirmed",
    )
    ReferenceMatch.objects.filter(pk=decision.pk).update(updated_at=decided)
    [before] = customer.get(f"{APP}/findings").json()

    decision.refresh_from_db()
    decision.save()  # what the next matching run does to the row
    assert decision.updated_at > decided
    [after] = customer.get(f"{APP}/findings").json()
    with override_settings(CASE_SYNC_ON_READ=False):
        [stored] = customer.get(f"{APP}/findings").json()

    assert before["confirmed_on"] == after["confirmed_on"] == stored["confirmed_on"] == "2026-09-01"
    assert CaseFinding.objects.get().verified_at == decided
