"""`discover_creator_sources`: targets only, prospects never, nothing spent (issue #118)."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog.models import Creator, WatchlistEntry
from marceline.tests.app_helpers import make_organisation, own
from matching.models import Reference

pytestmark = pytest.mark.django_db


def _rights_holder(name: str) -> Creator:
    """A rights holder of an owned original: the plainest way to become a target."""
    creator = Creator.objects.create(name=name)
    reference = Reference.objects.create(
        creator=creator, title=name, sha256=name.ljust(64, "0"), frame_fps=1
    )
    own(reference, make_organisation("Org"))
    return creator


def test_the_plan_names_targets_only_and_spends_nothing() -> None:
    _rights_holder("Target Person")
    prospect = Creator.objects.create(name="Prospect Person")
    WatchlistEntry.objects.create(
        creator=prospect, crawl=True, purpose=WatchlistEntry.Purpose.PROSPECT
    )
    out = StringIO()
    call_command("discover_creator_sources", stdout=out)
    text = out.getvalue()
    assert "Target Person" in text
    assert "Prospect Person" not in text
    assert "prospect(s) left out" in text
    assert "calls=0 cost=$0" in text


def test_the_german_market_is_planned_against_google_germany() -> None:
    _rights_holder("Target Person")
    out = StringIO()
    call_command("discover_creator_sources", "--market", "creator-de", stdout=out)
    assert "location=2276 language=de" in out.getvalue()


def test_it_refuses_when_nobody_asked_us_to_look() -> None:
    with pytest.raises(CommandError, match="No target creators"):
        call_command("discover_creator_sources")
