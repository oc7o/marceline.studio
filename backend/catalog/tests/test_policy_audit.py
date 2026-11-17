"""A policy switch cannot move without a record (tripwire T4, issue #106).

The change that prompted this was a queryset `.update()`, so that is the first test: the record is
written by the database, not by a model signal, and covers `.update()`, `.save()`, the admin and
raw SQL alike.
"""

import json
from pathlib import Path

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.forms import CheckboxInput
from django.http import HttpRequest
from django.test import RequestFactory
from django.utils import timezone

from catalog import policy_audit
from catalog.admin import SiteAdmin, SiteAdminForm
from catalog.models import Site, SitePolicyChange


def _changes(site: Site, field: str) -> list[SitePolicyChange]:
    return list(SitePolicyChange.objects.filter(site=site, field=field).order_by("changed_at"))


@pytest.mark.django_db
def test_a_bare_queryset_update_is_recorded_as_database(site: Site) -> None:
    """2026-09-18, the flip that started this: `.update()`, no signal, no record."""
    Site.objects.filter(pk=site.pk).update(capture_play_click=True)

    (change,) = _changes(site, "capture_play_click")
    assert (change.old_value, change.new_value) == ("false", "true")
    assert change.actor == policy_audit.UNATTRIBUTED_ACTOR
    assert change.reason == ""


@pytest.mark.django_db
def test_a_model_save_is_recorded_too(site: Site) -> None:
    site.enabled = False
    site.save()

    (change,) = _changes(site, "enabled")
    assert (change.old_value, change.new_value) == ("true", "false")


@pytest.mark.django_db
def test_the_actor_and_reason_are_recorded_when_someone_says_who_they_are(site: Site) -> None:
    with policy_audit.policy_actor("admin:operator", reason="bounded trial, issue #78"):
        Site.objects.filter(pk=site.pk).update(capture_enabled=True, capture_play_click=True)

    changes = SitePolicyChange.objects.filter(site=site)
    assert {change.field for change in changes} == {"capture_play_click"}
    assert all(change.actor == "admin:operator" for change in changes)
    assert all(change.reason == "bounded trial, issue #78" for change in changes)


@pytest.mark.django_db
def test_the_actor_does_not_leak_past_its_block(site: Site) -> None:
    with policy_audit.policy_actor("admin:operator", reason="first"):
        Site.objects.filter(pk=site.pk).update(daily_request_budget=2000)
    Site.objects.filter(pk=site.pk).update(daily_request_budget=3000)

    first, second = _changes(site, "daily_request_budget")
    assert first.actor == "admin:operator" and first.reason == "first"
    assert second.actor == policy_audit.UNATTRIBUTED_ACTOR and second.reason == ""


@pytest.mark.django_db
def test_an_unchanged_write_records_nothing(site: Site) -> None:
    Site.objects.filter(pk=site.pk).update(enabled=site.enabled, notes="a note")

    assert not SitePolicyChange.objects.exists()


@pytest.mark.django_db
def test_the_crawlers_pause_bookkeeping_is_not_recorded_but_a_humans_is(site: Site) -> None:
    """`paused_until` moves on every outage; only a human's pause belongs in this table."""
    Site.objects.filter(pk=site.pk).update(paused_until=timezone.now(), pause_reason="outage")
    assert not SitePolicyChange.objects.exists()

    with policy_audit.policy_actor("admin:operator", reason="site objected"):
        Site.objects.filter(pk=site.pk).update(pause_reason="operator hold")

    assert _changes(site, "pause_reason")


@pytest.mark.django_db
def test_age_confirmation_is_recorded_as_a_shape_and_never_as_a_cookie(site: Site) -> None:
    approval = {
        "cookies": {"age_verified": "yes-1234"},
        "approved_by": "operator",
        "approved_at": "2026-09-18",
    }
    site.age_confirmation = approval
    site.save()

    (change,) = _changes(site, "age_confirmation")
    assert change.old_value == "empty"
    assert change.new_value == policy_audit.age_confirmation_shape(approval)
    assert change.new_value == "approval, 1 cookie(s)"
    assert "age_verified" not in change.new_value
    assert "yes-1234" not in change.new_value


@pytest.mark.django_db
def test_the_log_is_append_only_in_the_database(site: Site) -> None:
    Site.objects.filter(pk=site.pk).update(enabled=False)
    change = SitePolicyChange.objects.get()

    with transaction.atomic(), pytest.raises(IntegrityError):
        SitePolicyChange.objects.filter(pk=change.pk).update(reason="rewritten")
    with transaction.atomic(), pytest.raises(IntegrityError):
        SitePolicyChange.objects.filter(pk=change.pk).delete()

    change.refresh_from_db()
    assert change.reason == ""


@pytest.mark.django_db
def test_raw_sql_cannot_slip_past_the_trigger(site: Site) -> None:
    with connection.cursor() as cursor:
        cursor.execute("UPDATE catalog_site SET capture_play_click = true WHERE id = %s", [site.pk])

    assert _changes(site, "capture_play_click")


@pytest.mark.django_db
def test_the_checked_helper_records_and_refuses_a_field_that_is_not_policy(site: Site) -> None:
    updated = policy_audit.apply_policy_update(
        site, actor="manage.py seed_sources", reason="sources.md sha256:abc", enabled=False
    )

    assert updated == 1 and site.enabled is False
    (change,) = _changes(site, "enabled")
    assert change.actor == "manage.py seed_sources"
    with pytest.raises(ValueError):
        policy_audit.apply_policy_update(site, actor="x", notes="nope")


@pytest.mark.django_db
def test_recent_changes_is_what_make_status_prints(site: Site) -> None:
    Site.objects.filter(pk=site.pk).update(capture_play_click=True)

    summary = policy_audit.recent_changes(timezone.now(), days=7)

    assert summary.total == 1
    assert "capture_play_click" in policy_audit.describe(summary.rows[0])
    assert str(site.pk) in policy_audit.describe(summary.rows[0])


@pytest.mark.django_db
def test_seed_sources_records_itself_with_the_revision_of_the_file(tmp_path: Path) -> None:
    """`seed_sources` re-applies the reviewed crawl policy, so it has to say who it is."""
    site = Site.objects.create(domain="site01.example", enabled=False)  # a reviewed SitePolicy domain
    sources = tmp_path / "sources.md"
    sources.write_text("Sites:\n- site01.example\n\nCreators:\n- Someone\n")

    call_command("seed_sources", path=str(sources))

    (change,) = _changes(site, "enabled")
    assert change.actor == "seed_sources"
    assert change.reason.startswith("sources.md sha256:")
    assert (change.old_value, change.new_value) == ("false", "true")


def _staff_request(user: AbstractBaseUser) -> HttpRequest:
    request = RequestFactory().post("/admin/catalog/site/")
    request.user = user  # type: ignore[assignment]
    return request


def _admin_form(site: Site, user: AbstractBaseUser, **changes: object) -> SiteAdminForm:
    """The real admin form for this site, with everything at its current value but `changes`.

    Built through `ModelAdmin.get_form` so that the readonly fields the admin excludes are excluded
    here too; a change whose value is `None` is an unticked checkbox.
    """
    form_class = SiteAdmin(Site, AdminSite()).get_form(_staff_request(user), obj=site, change=True)
    unbound = form_class(instance=site)
    data: dict[str, object] = {}
    for name, field in unbound.fields.items():
        value = unbound.get_initial_for_field(field, name)
        if isinstance(field.widget, CheckboxInput):
            if value:
                data[name] = "on"
        elif value is None:
            data[name] = ""
        elif isinstance(value, dict | list):
            data[name] = json.dumps(value)
        else:
            data[name] = str(value)
    for name, value in changes.items():
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value
    return form_class(data, instance=site)  # type: ignore[return-value]


@pytest.fixture
def staff(db: None) -> AbstractBaseUser:
    return get_user_model().objects.create_superuser("operator", "o@example.test", "x")


@pytest.mark.django_db
def test_the_admin_refuses_a_weighty_switch_without_a_reason(
    site: Site, staff: AbstractBaseUser
) -> None:
    form = _admin_form(site, staff, capture_play_click="on")

    assert not form.is_valid()
    assert "policy_reason" in form.errors

    with_reason = _admin_form(
        site,
        staff,
        capture_play_click="on",
        policy_reason="bounded 15-visit trial, issue #78",
    )
    assert with_reason.is_valid(), with_reason.errors


@pytest.mark.django_db
def test_the_admin_asks_for_a_reason_only_when_a_budget_goes_up(
    site: Site, staff: AbstractBaseUser
) -> None:
    lower = _admin_form(site, staff, daily_request_budget=site.daily_request_budget - 100)
    raise_it = _admin_form(site, staff, daily_request_budget=site.daily_request_budget + 100)

    assert lower.is_valid(), lower.errors
    assert not raise_it.is_valid()
    assert "policy_reason" in raise_it.errors


@pytest.mark.django_db
def test_the_admin_saves_with_the_user_as_the_actor(site: Site, staff: AbstractBaseUser) -> None:
    form = _admin_form(site, staff, enabled=None, policy_reason="the site objected")
    assert form.is_valid(), form.errors

    SiteAdmin(Site, AdminSite()).save_model(
        _staff_request(staff), form.save(commit=False), form, change=True
    )

    (change,) = _changes(site, "enabled")
    assert change.actor == "admin:operator"
    assert change.reason == "the site objected"
