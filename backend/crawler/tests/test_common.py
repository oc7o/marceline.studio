"""Shared adapter helpers (crawler/sites/common.py)."""

import pytest

from catalog.models import Site
from crawler.models import CrawlTask
from crawler.sites.common import normalize_name, slugify
from crawler.sites.registry import ADAPTERS


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Eléna_Märsh", "elena marsh"),
        ("Heiße Getränke", "heisse getranke"),
        ("GROẞE Straße", "grosse strasse"),
        ("Österreich Übung", "osterreich ubung"),
        ("日本語", ""),
    ],
)
def test_normalize_name_keeps_german_letters(text: str, expected: str) -> None:
    assert normalize_name(text) == expected


def test_slugify_german_title() -> None:
    assert slugify("Süße Grüße aus Köln!") == "susse-grusse-aus-koln"


def test_german_search_seeds_on_every_search_adapter() -> None:
    """A German free-text query (e.g. a studio or series name) never loses letters in a seed URL."""
    query = "Heiße Grüße"

    for domain, adapter in ADAPTERS.items():
        tasks = adapter.search_tasks(Site(domain=domain), query)
        for task in tasks:
            url = task.url.lower()
            assert task.kind == CrawlTask.Kind.SEARCH
            assert "heisse" in url, domain
            assert "grusse" in url, domain
            assert "heie" not in url, domain
