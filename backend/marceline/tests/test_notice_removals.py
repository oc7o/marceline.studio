"""Stamping when a notice's content was first observed gone (ADR 0027 amendment).

The stamp comes from availability checks and case findings, never from a typed date, is written
once, and only when every page the notice named is gone.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from catalog.models import HosterContact, HostingProvider
from index.models import Page
from marceline.hosting import first_removal_after, observe_notice_removals
from matching.models import CaseFinding, LeakCase, Reference

SENT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
REMOVED = Page.Availability.REMOVED


@pytest.fixture
def provider(db: None) -> HostingProvider:
    return HostingProvider.objects.create(asn=24940, name="HETZNER-AS")


def notice(provider: HostingProvider, *pages: Page, sent_at: datetime = SENT) -> HosterContact:
    contact = HosterContact.objects.create(
        provider=provider, channel=HosterContact.Channel.EMAIL, sent_at=sent_at
    )
    for page in pages:
        page.notices.add(contact)
    return contact


def gone(page: Page, checked_at: datetime) -> Page:
    page.availability = REMOVED
    page.availability_checked_at = checked_at
    page.save(update_fields=["availability", "availability_checked_at"])
    return page


def test_first_removal_after_prefers_the_case_finding_stamp(
    make_page: Callable[[str], Page],
) -> None:
    page = make_page("a")
    assert first_removal_after(SENT, page, []) is None  # not removed

    gone(page, SENT + 3 * DAY)
    assert first_removal_after(SENT, page, []) == SENT + 3 * DAY
    assert first_removal_after(SENT, page, [SENT + 2 * DAY, SENT + 5 * DAY]) == SENT + 2 * DAY
    # A finding stamp from before the notice does not count; the page's own check stands in.
    assert first_removal_after(SENT, page, [SENT - DAY]) == SENT + 3 * DAY
    # Gone before the notice went out: tells nothing about the provider.
    assert first_removal_after(SENT + 4 * DAY, page, [SENT + 2 * DAY]) is None


def test_a_notice_is_stamped_when_its_last_page_is_observed_gone(
    provider: HostingProvider, make_page: Callable[[str], Page], reference: Reference
) -> None:
    first, second = make_page("first"), make_page("second")
    contact = notice(provider, first, second)
    unlinked = notice(provider)

    gone(first, SENT + DAY)
    assert observe_notice_removals() == 0  # the second page is still up
    contact.refresh_from_db()
    assert contact.removed_observed_at is None

    gone(second, SENT + 4 * DAY)
    case = LeakCase.objects.create(reference=reference, opened_at=SENT - DAY)
    CaseFinding.objects.create(
        case=case, page=second, discovered_at=SENT - DAY, removed_at=SENT + 3 * DAY
    )
    assert observe_notice_removals() == 1
    contact.refresh_from_db()
    assert contact.removed_observed_at == SENT + 3 * DAY  # the finding's first observation

    # Written once: a later check or a re-upload does not move it, and nothing is re-stamped.
    gone(first, SENT + 9 * DAY)
    assert observe_notice_removals() == 0
    contact.refresh_from_db()
    assert contact.removed_observed_at == SENT + 3 * DAY
    unlinked.refresh_from_db()
    assert unlinked.removed_observed_at is None


def test_a_page_already_gone_when_the_notice_went_out_gives_no_stamp(
    provider: HostingProvider, make_page: Callable[[str], Page]
) -> None:
    page = gone(make_page("early"), SENT - DAY)
    contact = notice(provider, page)

    assert observe_notice_removals() == 0
    contact.refresh_from_db()
    assert contact.removed_observed_at is None
