"""The adapter drafting harness against the recorded fixtures (ADR 0043, issue #21).

The fixtures of the sites that already have adapters are ground truth: the draft has to reach the
same conclusions a person reached by hand, or it is not worth reading.
"""

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from selectolax.lexbor import LexborHTMLParser

from crawler.drafting import (
    DETAIL,
    LISTING,
    MISSING,
    LinkFamily,
    _anchors,
    _duration_seconds,
    analyse,
    classify,
    draft_lines,
    link_families,
    load_fixtures,
    model_index,
)
from crawler.sites.kvs import KVS_SITES, SITE08
from crawler.sites.site01 import SITE01
from crawler.tests.helpers import FIXTURES

KVS_DOMAINS = frozenset({site.domain for site in KVS_SITES} | {SITE01.domain, SITE08.domain})


def _fixture_directories() -> list[Path]:
    return [
        directory
        for directory in sorted(FIXTURES.iterdir())
        if directory.is_dir() and list(directory.glob("*.html")) and "." in directory.name
    ]


def _anchors_of(domain: str, name: str) -> list[Any]:
    html = (FIXTURES / domain / name).read_text()
    return _anchors(LexborHTMLParser(html), f"https://{domain}/")


def _family(template: str, families: list[LinkFamily]) -> LinkFamily:
    matching = [family for family in families if family.template == template]
    assert matching, [family.template for family in families]
    return matching[0]


@pytest.mark.parametrize("directory", _fixture_directories(), ids=lambda path: path.name)
def test_the_engine_fingerprint_agrees_with_the_registry(directory: Path) -> None:
    # Every site whose adapter is a KVS_SITES entry is recognised from its markup alone, and no
    # site with a bespoke adapter is mistaken for one: 9 of 9 KVS sites, 0 of 15 bespoke ones.
    draft = analyse(directory.name, load_fixtures(directory))

    assert (draft.engine == "kvs") is (directory.name in KVS_DOMAINS)


@pytest.mark.parametrize("directory", _fixture_directories(), ids=lambda path: path.name)
def test_every_recorded_site_drafts_without_crashing(directory: Path) -> None:
    lines = draft_lines(directory)

    assert lines[0].startswith(f"{directory.name}: ")
    assert "next (CLAUDE.md, Adding a site adapter)" in lines


def test_the_draft_reproduces_a_committed_kvs_entry() -> None:
    # site16.example's KvsSite was written by hand from these fixtures (commit 486afdc).
    committed = next(site for site in KVS_SITES if site.domain == "site16.example")

    draft = analyse(committed.domain, load_fixtures(FIXTURES / committed.domain))

    assert draft.engine == "kvs"
    assert draft.detail_regex == committed.video_path
    assert draft.host == committed.host
    assert f'search_path="{committed.search_path}"' in "\n".join(
        draft_lines(FIXTURES / draft.domain)
    )


def test_a_language_prefixed_catalogue_keeps_its_sections_apart() -> None:
    # `/de/videos/<id>/<slug>/` and `/de/kategorien/<slug>/<page>/` are the same depth; grouping by
    # the first segment alone would merge them into one useless `/de/*/*/*/`.
    families = link_families(_anchors_of("site16.example", "listing.html"))

    videos = _family("/de/videos/*/*/", families)
    assert videos.regex == r"^/de/videos/\d+/[^/]+/?$"
    assert videos.with_text == videos.count  # every card carries its title (ADR 0029's gate)
    assert "data-original" in videos.image_attributes


def test_a_performer_index_is_found_from_one_link() -> None:
    # A video credits one or two performers, so the index is found by the segment that names it.
    anchors = _anchors_of("site14.example", "performer.html")

    assert model_index(anchors)[0][0] == "/performer/{slug}/"


def test_pages_are_told_apart() -> None:
    for name, expected in (
        ("video.html", DETAIL),
        ("listing.html", LISTING),
        ("missing.html", MISSING),
    ):
        html = (FIXTURES / "site14.example" / name).read_text()
        tree = LexborHTMLParser(html)

        kind, _ = classify(tree, _anchors_of("site14.example", name))

        assert kind == expected, name


def test_a_duration_a_theme_writes_without_its_leading_p_is_reported_not_hidden() -> None:
    # Two live adapters had to repair exactly this; the draft is where it should be noticed.
    assert _duration_seconds("T6M15S") == (375, "ISO 8601 without the leading P")
    assert _duration_seconds("PT6M15S") == (375, "")
    assert _duration_seconds("6:15") == (375, "clock format, not ISO 8601")
    assert _duration_seconds("soon") == (None, "")

    draft = analyse("site14.example", load_fixtures(FIXTURES / "site14.example"))

    notes = [candidate.note for candidate in draft.fields["duration"]]
    assert "ISO 8601 without the leading P" in notes


def test_the_draft_prints_structure_and_never_the_page_s_own_words() -> None:
    # The rule this module exists under ("findings name identifiers, never content"): a reviewer
    # reads the value in the fixture, never here. Checked against the fixtures' own strings, so
    # nothing quotable has to live in this file.
    for domain in ("site14.example", "site23.example", "site16.example"):
        output = "\n".join(draft_lines(FIXTURES / domain))
        tree = LexborHTMLParser((FIXTURES / domain / "video.html").read_text())
        for node in (tree.css_first("title"), tree.css_first("h1")):
            value = " ".join(node.text().split()) if node else ""
            assert len(value) > 10, domain
            assert value not in output, domain


def test_the_command_drafts_a_recorded_site() -> None:
    out = StringIO()

    call_command("draft_adapter", "--domain", "site01.example", stdout=out)

    printed = out.getvalue()
    assert printed.startswith("site01.example: kvs")
    assert "already has a registered adapter" in printed


def test_the_command_refuses_what_it_cannot_draft(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="exactly one"):
        call_command("draft_adapter")
    with pytest.raises(CommandError, match="no fixture directory"):
        call_command("draft_adapter", "--domain", "nothing.example")
    with pytest.raises(CommandError, match="record them first"):
        call_command("draft_adapter", "--fixtures", str(tmp_path))
