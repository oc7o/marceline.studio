"""A long-lived worker still writing the previous code must not lose rows.

On 2026-09-15 a migration added `Page.availability` (NOT NULL, `default=`) while the crawler
containers kept running the code from before it. Their INSERTs did not name the column, Postgres
had no default for it, and 997 detail pages were lost to
`null value in column "availability" violates not-null constraint`. The rule that came out of it —
restart the workers after every migration — is operational, so it can be forgotten; this is the
same rule in the schema.

Two things are pinned here:

- **The invariant.** On every table a long-lived process inserts into, a NOT NULL column that has
  a Python `default=` also has a `db_default=`. The author of such a column already decided what
  an unspecified row should hold, so the database can fill it, and a process that predates the
  column keeps writing. Columns with no default at all (a page's `url`, a task's `site_id`) are
  values the code must always supply and are deliberately not covered — they are the "always
  supplied" set below.
- **The behaviour.** A raw INSERT that names only the always-supplied columns — the narrowest
  thing a stale worker can send — succeeds and the row comes back with the model's own defaults.
  `marceline/tests/test_app_public_ids.py` pins the same for the generated public ids.
"""

from typing import Any

import pytest
from django.apps import apps
from django.db import connection
from django.db.models import NOT_PROVIDED, Field, Model

from catalog.models import Site
from crawler.models import CrawlTask
from index.models import Page

#: Tables a process that runs for days inserts into: the crawler and media workers, the crawl
#: runtime's lanes (ADR 0030), the capture worker and the scheduler. A new one belongs here.
#:
#: `SafetyIncident` and `SafetyIncidentPage` are on the list because the crawler reaches them:
#: `store_parse_result` sends `index.signals.safety_rules_applied` on commit and
#: `marceline.safety_rules.follow_up` opens the case from there. `SafetyIncidentHash` is not —
#: `follow_up` calls `sync_incidents(pages=…)` with no `hashes=`, so only `safety_scan`, a
#: short-lived command, ever writes one.
WORKER_WRITTEN = (
    "core.HostSlot",
    "crawler.CrawlTask",
    "crawler.LLMCall",
    "index.FrameHash",
    "index.Media",
    "index.Page",
    "index.SafetyIncident",
    "index.SafetyIncidentPage",
    "index.SafetyRuleDecision",
    "index.SiteCreatorBreadcrumb",
)


def _not_null_fields(model: type[Model]) -> list[Field[Any, Any]]:
    return [field for field in model._meta.local_fields if not field.primary_key and not field.null]


def always_supplied(model: type[Model]) -> list[Field[Any, Any]]:
    """NOT NULL columns with no default of any kind: the code must name them in every INSERT."""
    return [
        field
        for field in _not_null_fields(model)
        if not field.has_default() and field.db_default is NOT_PROVIDED
    ]


@pytest.mark.parametrize("label", WORKER_WRITTEN)
def test_a_defaulted_column_is_defaulted_in_the_database_too(label: str) -> None:
    """`default=` without `db_default=` is the 997-lost-pages bug waiting to happen again."""
    model = apps.get_model(label)
    missing = [
        field.name
        for field in _not_null_fields(model)
        if field.has_default() and field.db_default is NOT_PROVIDED
    ]

    assert missing == [], (
        f"{label}: {', '.join(missing)} have a Python default but no db_default, so a worker "
        "process that predates them cannot insert. Add db_default= (see this module's docstring)."
    )


def _insert(model: type[Model], values: dict[str, Any]) -> int:
    """Raw INSERT naming exactly `values`: what a worker's stale code would send."""
    columns = ", ".join(f'"{name}"' for name in values)
    placeholders = ", ".join(["%s"] * len(values))
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {model._meta.db_table} ({columns}) VALUES ({placeholders}) RETURNING id",
            list(values.values()),
        )
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def test_a_stale_crawler_can_still_insert_a_page(db: None) -> None:
    site = Site.objects.create(domain="stale.test", name="Stale")
    columns = {field.column for field in always_supplied(Page)}
    assert columns == {
        "site_id",
        "url",
        "title",
        "first_seen",
        "last_seen",
        "safety_note",
        "safety_score_reason",
        "safety_rule_version",
        "inactive_reason",
    }, "the set the crawler must always name changed: check the raw INSERT below"

    page_id = _insert(
        Page,
        {
            "site_id": site.pk,
            "url": "https://stale.test/one",
            "title": "",
            "first_seen": "2026-09-17T00:00:00Z",
            "last_seen": "2026-09-17T00:00:00Z",
            "safety_note": "",
            "safety_score_reason": "",
            "safety_rule_version": "",
            "inactive_reason": "",
        },
    )

    page = Page.objects.get(pk=page_id)
    assert page.availability == Page.Availability.UNKNOWN
    assert page.safety_status == Page.SafetyStatus.CLEAR
    assert (page.safety_matches, page.safety_score_signals, page.tags) == ([], [], [])
    assert page.raw_extraction == {}
    assert page.safety_release == Page.SafetyRelease.NONE
    assert page.active is True


def test_a_stale_crawler_can_still_enqueue_a_task(db: None) -> None:
    site = Site.objects.create(domain="stale.test", name="Stale")
    columns = {field.column for field in always_supplied(CrawlTask)}
    assert columns == {
        "site_id",
        "url",
        "kind",
        "decision_reason",
        "last_error",
        "created_at",
        "updated_at",
    }, "the set the crawler must always name changed: check the raw INSERT below"

    task_id = _insert(
        CrawlTask,
        {
            "site_id": site.pk,
            "url": "https://stale.test/videos/",
            "kind": CrawlTask.Kind.LISTING,
            "decision_reason": "",
            "last_error": "",
            "created_at": "2026-09-17T00:00:00Z",
            "updated_at": "2026-09-17T00:00:00Z",
        },
    )

    task = CrawlTask.objects.get(pk=task_id)
    assert task.status == CrawlTask.Status.PENDING
    assert (task.priority, task.attempts, task.context) == (0, 0, {})
