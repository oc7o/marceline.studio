"""A site operator's objection: recorded, and applied through the existing switches (#108)."""

from datetime import date, timedelta

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import RequestFactory
from django.utils import timezone

from catalog import policy_audit
from catalog.admin import SiteObjectionAdmin
from catalog.models import Site, SiteObjection, SitePolicyChange
from catalog.site_objections import PAUSE_DAYS, apply_decision, changes_for, reason_text

pytestmark = pytest.mark.django_db


def objection(decision: str, *, reason: str = "", domain: str = "example.test") -> SiteObjection:
    site = Site.objects.create(domain=domain, daily_media_request_budget=1000)
    return SiteObjection.objects.create(
        site=site,
        received_on=date(2026, 9, 18),
        kind=SiteObjection.Kind.LEGAL_LETTER,
        channel=SiteObjection.Channel.EMAIL,
        decision=decision,
        decision_reason=reason,
    )


def test_disabling_stops_the_crawl_and_records_why() -> None:
    row = objection(SiteObjection.Decision.DISABLED)
    changes = apply_decision(row, actor="operator")
    row.site.refresh_from_db()
    row.refresh_from_db()
    assert changes == {"enabled": False}
    assert row.site.enabled is False
    assert row.applied_at is not None and row.decided_at is not None
    assert row.decided_by == "operator"


def test_pausing_is_reversible_and_carries_the_objection_as_its_reason() -> None:
    row = objection(SiteObjection.Decision.PAUSED)
    now = timezone.now()
    apply_decision(row, actor="operator", now=now)
    row.site.refresh_from_db()
    assert row.site.paused_until == now + timedelta(days=PAUSE_DAYS)
    assert row.site.enabled is True
    assert f"#{row.pk}" in row.site.pause_reason
    assert "legal_letter" in row.site.pause_reason


def test_stopping_media_leaves_the_text_index_alone() -> None:
    row = objection(SiteObjection.Decision.MEDIA_STOPPED)
    apply_decision(row, actor="operator")
    row.site.refresh_from_db()
    assert row.site.daily_media_request_budget == 0
    assert row.site.enabled is True


def test_pending_and_declined_change_no_switch() -> None:
    for decision, reason in (
        (SiteObjection.Decision.PENDING, ""),
        (SiteObjection.Decision.DECLINED, "no basis given, no takedown asked for"),
    ):
        row = objection(decision, reason=reason, domain=f"{decision}.test")
        assert apply_decision(row, actor="operator") == {}
        row.site.refresh_from_db()
        assert (row.site.enabled, row.site.paused_until) == (True, None)
        assert row.site.daily_media_request_budget == 1000


def test_applying_twice_changes_the_same_thing_twice() -> None:
    row = objection(SiteObjection.Decision.DISABLED)
    assert apply_decision(row, actor="operator") == changes_for(row, now=timezone.now())
    assert apply_decision(row, actor="someone else") == {"enabled": False}
    row.refresh_from_db()
    # The first decider is kept: applying again is not deciding again.
    assert row.decided_by == "operator"


def test_declining_without_a_reason_is_refused_by_the_database() -> None:
    site = Site.objects.create(domain="declined.test")
    with pytest.raises(IntegrityError), transaction.atomic():
        SiteObjection.objects.create(
            site=site,
            received_on=date(2026, 9, 18),
            decision=SiteObjection.Decision.DECLINED,
        )


def test_the_reason_text_names_no_person() -> None:
    row = objection(SiteObjection.Decision.PAUSED)
    text = reason_text(row)
    assert str(row.pk) in text and "2026-09-18" in text
    assert "@" not in text


def test_an_objection_can_never_be_deleted_in_the_admin() -> None:
    """A record, like `DoNotContactRecord`: the answer survives the person who gave it."""
    admin = SiteObjectionAdmin(SiteObjection, AdminSite())
    request = RequestFactory().get("/")
    request.user = User(is_superuser=True, is_staff=True)
    assert admin.has_delete_permission(request) is False
    assert "delete_selected" not in dict(admin.get_actions(request))


def test_a_site_with_an_objection_cannot_be_deleted() -> None:
    """`PROTECT`: losing the site row would lose the record of who asked us to stop."""
    row = objection(SiteObjection.Decision.DISABLED)
    with pytest.raises(ProtectedError), transaction.atomic():
        row.site.delete()


@pytest.mark.parametrize(
    ("decision", "field"),
    (
        (SiteObjection.Decision.DISABLED, "enabled"),
        (SiteObjection.Decision.MEDIA_STOPPED, "daily_media_request_budget"),
        (SiteObjection.Decision.PAUSED, "paused_until"),
    ),
)
def test_every_switch_reaches_the_policy_audit_with_the_objection_as_its_reason(
    decision: str, field: str
) -> None:
    """Issue #106: a policy switch nobody can explain is the state both records exist to end."""
    row = objection(decision, domain=f"{decision}-audit.test")
    apply_decision(row, actor="operator")

    changes = SitePolicyChange.objects.filter(site=row.site, field=field)
    recorded = changes.get()
    assert recorded.actor == f"objection:{row.pk}"
    assert str(row.pk) in recorded.reason
    # Not `database`: that is what an unattributed write records, and it is itself a finding.
    assert recorded.actor != policy_audit.UNATTRIBUTED_ACTOR
