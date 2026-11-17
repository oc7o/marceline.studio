"""The pure half of `make status` (`marceline/project_status.py`).

The gap detector is the part that must never silently pass: it is the only automatic check that a
setting `settings.py` reads is actually handed to a container, and both bugs it exists for looked
exactly like a green run. So the tests here assert on the real repository files as well as on
fixtures — a rule that stops recognising `env_bool(...)` or an `environment:` key would still find
its fixtures, but not the 90-odd settings the real files hold.
"""

from pathlib import Path

import pytest
from django.utils import timezone

from catalog.models import Site
from index.models import Page
from index.retention import scrub_deactivated_pages
from marceline.management.commands.project_status import Command as ProjectStatusCommand
from marceline.project_status import (
    compose_env_names,
    compose_service_names,
    dotenv_names,
    env_gaps,
    flag_value,
    format_age,
    settings_env_names,
    truncate,
    wrap,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SETTINGS = REPO_ROOT / "backend" / "marceline" / "settings.py"
COMPOSE = REPO_ROOT / "infra" / "compose.yaml"

SETTINGS_SAMPLE = """
DJANGO_ENV = env_str("DJANGO_ENV", default="dev")
DEBUG = env_bool("DJANGO_DEBUG", default=True)
PORT = env_int("POSTGRES_PORT", default=5433)
DELAY = env_float("CDN_HOST_DELAY_S", default=1.0)
ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
if env_is_set("EMAIL_HOST"):
    pass
MATCHING_METADATA_CANDIDATES_ENABLED = env_bool(
    "MATCHING_METADATA_CANDIDATES_ENABLED", default=False
)
# Not an environment read at all, and must not be mistaken for one:
LOCAL = some_other_call("NOT_AN_ENV_VAR")
"""

COMPOSE_SAMPLE = """
name: marceline

x-mail-env: &mail-env
  EMAIL_HOST: ${EMAIL_HOST:-}

services:
  db:
    image: postgres:17
    environment:
      POSTGRES_PORT: "5432"

  web:
    build: ../backend
    command: python manage.py runserver
    environment:
      <<: *mail-env
      DJANGO_ENV: dev
      DJANGO_DEBUG: "true"
    ports:
      - "${WEB_BIND:-127.0.0.1}:8000:8000"

  crawler-site01: &crawler
    command: python manage.py crawl_worker --site site01.example
    profiles: ["workers"]
"""


def test_settings_reads_every_core_env_helper() -> None:
    names = settings_env_names(SETTINGS_SAMPLE)
    assert names == {
        "DJANGO_ENV",
        "DJANGO_DEBUG",
        "POSTGRES_PORT",
        "CDN_HOST_DELAY_S",
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        "EMAIL_HOST",
        "MATCHING_METADATA_CANDIDATES_ENABLED",
    }


def test_a_multiline_env_call_is_still_found() -> None:
    """`MATCHING_METADATA_CANDIDATES_ENABLED` is written across two lines in the real settings."""
    assert "MATCHING_METADATA_CANDIDATES_ENABLED" in settings_env_names(
        'X = env_bool(\n    "MATCHING_METADATA_CANDIDATES_ENABLED", default=False\n)'
    )


def test_compose_env_names_include_anchor_blocks_and_exclude_schema_keys() -> None:
    names = compose_env_names(COMPOSE_SAMPLE)
    assert names == {"EMAIL_HOST", "POSTGRES_PORT", "DJANGO_ENV", "DJANGO_DEBUG"}
    assert not names & {"image", "build", "command", "profiles", "ports", "environment"}


def test_compose_service_names_are_the_top_level_services() -> None:
    assert compose_service_names(COMPOSE_SAMPLE) == {"db", "web", "crawler-site01"}


def test_dotenv_names_ignore_comments_and_keep_empty_values() -> None:
    text = "# a comment\nFOO=1\nexport BAR=\n\nnot an assignment\nBAZ = 2\n"
    assert dotenv_names(text) == {"FOO", "BAR", "BAZ"}


def test_a_setting_set_in_dotenv_but_passed_by_no_service_is_silent() -> None:
    """The exact shape of both day-long bugs: `.env` sets it, no container ever sees it."""
    gaps = env_gaps(
        SETTINGS_SAMPLE,
        COMPOSE_SAMPLE,
        dotenv_text="DJANGO_CSRF_TRUSTED_ORIGINS=http://example.test\nCDN_HOST_DELAY_S=2\n",
    )
    assert gaps.silent == ("CDN_HOST_DELAY_S", "DJANGO_CSRF_TRUSTED_ORIGINS")
    assert "MATCHING_METADATA_CANDIDATES_ENABLED" in gaps.default_only


def test_a_setting_compose_passes_is_not_a_gap() -> None:
    gaps = env_gaps(SETTINGS_SAMPLE, COMPOSE_SAMPLE, dotenv_text="DJANGO_ENV=dev\n")
    assert gaps.silent == ()
    assert "DJANGO_ENV" not in gaps.default_only + gaps.prod_only


def test_a_production_only_setting_is_split_out_rather_than_alarming() -> None:
    prod = "services:\n  web:\n    environment:\n      CDN_HOST_DELAY_S: '2'\n"
    gaps = env_gaps(SETTINGS_SAMPLE, COMPOSE_SAMPLE, prod_compose_text=prod)
    assert gaps.prod_only == ("CDN_HOST_DELAY_S",)
    assert "CDN_HOST_DELAY_S" not in gaps.default_only


def test_gap_totals_add_up() -> None:
    gaps = env_gaps(SETTINGS_SAMPLE, COMPOSE_SAMPLE, dotenv_text="CDN_HOST_DELAY_S=2\n")
    assert gaps.total == len(gaps.silent) + len(gaps.prod_only) + len(gaps.default_only)
    assert gaps.total == len(
        settings_env_names(SETTINGS_SAMPLE) - compose_env_names(COMPOSE_SAMPLE)
    )


def test_the_detector_reads_the_real_repository_files() -> None:
    """A rule that stopped matching would report a tidy, empty, wrong answer — so assert volume.

    The numbers are floors, not fixtures: they only have to be far enough above zero that a broken
    regex (which yields an empty set on both sides, and so no gaps at all) cannot pass.
    """
    settings_text, compose_text = SETTINGS.read_text(), COMPOSE.read_text()
    read = settings_env_names(settings_text)
    passed = compose_env_names(compose_text)
    assert len(read) > 60
    assert len(passed) > 15
    # Settings whose absence from compose was a real, day-long bug, and which must therefore be
    # things this detector can see at all.
    assert {"DJANGO_CSRF_TRUSTED_ORIGINS", "BROWSER_CAPTURE_ENABLED"} <= read
    assert {"POSTGRES_DB", "CRAWLER_USER_AGENT"} <= passed
    assert len(compose_service_names(compose_text)) > 20


def test_the_settings_it_reads_are_a_superset_of_what_compose_passes() -> None:
    """Every variable the development compose file passes is one settings.py actually reads.

    The other direction is the gap report; this direction catches a compose key that no longer
    has a reader — a setting renamed in `settings.py` while `infra/compose.yaml` kept the old
    name, which would look configured and do nothing.
    """
    read = settings_env_names(SETTINGS.read_text())
    passed = compose_env_names(COMPOSE.read_text())
    # Passed to containers for reasons outside settings.py: Postgres's own image variables, and
    # `PYTHONDONTWRITEBYTECODE`, which the upload decoder's container needs because its root
    # filesystem is read-only and the decode child runs with `RLIMIT_FSIZE = 0` (ADR 0044) — an
    # interpreter switch, not a Django setting.
    infrastructure = {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "PYTHONDONTWRITEBYTECODE",
    }
    assert passed - read <= infrastructure, "compose passes a variable settings.py never reads"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "now"), (59, "now"), (60, "1m"), (3599, "59m"), (3600, "1h"), (86400, "1d"), (-5, "now")],
)
def test_format_age(seconds: int, expected: str) -> None:
    assert format_age(seconds) == expected


def test_truncate_flattens_and_marks_what_it_cut() -> None:
    assert truncate("a  b\nc", 10) == "a b c"
    assert truncate("abcdefghij", 5) == "abcd…"


def test_wrap_breaks_on_spaces_and_never_loses_a_word() -> None:
    words = "alpha beta gamma delta epsilon"
    lines = wrap(words, 12)
    assert lines == ["alpha beta", "gamma delta", "epsilon"]
    assert " ".join(lines) == words
    assert wrap("", 10) == []
    # A word longer than the width gets its own line rather than being cut.
    assert wrap("short supercalifragilistic", 10) == ["short", "supercalifragilistic"]


def test_flag_value_separates_absent_from_empty() -> None:
    assert flag_value(None) == "not passed"
    assert flag_value("") == "EMPTY"
    assert flag_value("  ") == "EMPTY"
    assert flag_value("true") == "true"


@pytest.mark.django_db
def test_the_page_scrub_line_names_the_gap_and_the_command(site: Site) -> None:
    """The one number issue #148 exists for: deactivated pages that still hold their text.

    `make status` is where a regression has to be visible, because the documents that state
    this deletion are read far more often than the query behind them is run.
    """
    command = ProjectStatusCommand()
    assert "0 of 0 deactivated page(s)" in command._page_scrub()

    now = timezone.now()
    page = Page.objects.create(
        site=site,
        url=f"https://{site.domain}/untargeted",
        title="a video",
        active=False,
        inactive_reason=Page.InactiveReason.UNTARGETED,
    )
    Page.objects.filter(pk=page.pk).update(active_changed_at=now, deactivated_at=now)

    line = command._page_scrub()

    assert "1 of 1 deactivated page(s) STILL HOLD" in line
    assert "scrub_inactive_pages" in line and "#148" in line

    scrub_deactivated_pages([page.pk])

    assert "0 of 1 deactivated page(s) still hold" in command._page_scrub()
