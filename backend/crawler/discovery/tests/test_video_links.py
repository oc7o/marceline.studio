"""Issue #23: the probe's video-link recognizer, measured against real recorded homepages.

Pure function, no database, no network: `crawler.discovery.video_links.video_detail_paths` takes
HTML and the page's own URL and returns the paths it thinks are one video's own page.

`_old_heuristic` is the retired substring check `probe.py` carried before this fix (`/video/`,
`/videos/`, `/watch/`, `/watch?` verbatim) — kept here only as the regression guard's baseline, not
imported from anywhere live. Measured over every recorded homepage/listing fixture we have
(`crawler/tests/fixtures/*/home.html`, `*/listing*.html`, 25 files, 15 domains): the old heuristic
scored zero video links on 3 of them (`site02.example` — `.html` detail pages with a leading id;
`site07.example` — an id glued onto the end of a slug with no separating word; `site03.example` — a bare
hex id under `/post/`), all three of which reproduce the exact gap that left source-discovery
candidates 38 and 117 under-counted on issue #23. The new function scores zero on 1 of the 25
(`site14.example`, whose detail path `/filme/<slug>/` carries no id anywhere at all — a gap no
id-based heuristic can close, and not a regression: the old heuristic scored zero here too).
"""

from pathlib import Path

from selectolax.lexbor import LexborHTMLParser

from crawler.discovery.video_links import video_detail_paths

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
LISTING_FIXTURES = sorted(FIXTURES.glob("*/home.html")) + sorted(FIXTURES.glob("*/listing*.html"))


def _old_heuristic(html: str) -> int:
    """The substring check this module replaces — a fixed point for the regression guard only."""
    tree = LexborHTMLParser(html)
    return len(
        {
            node.attributes.get("href", "")
            for node in tree.css("a[href]")
            if any(
                marker in (node.attributes.get("href") or "").lower()
                for marker in ("/video/", "/videos/", "/watch/", "/watch?")
            )
        }
    )


def test_fixture_count() -> None:
    """Sanity: the fixture set this test measures against hasn't silently shrunk or grown."""
    assert len(LISTING_FIXTURES) == 25


def test_new_heuristic_scores_fewer_fixtures_zero_than_the_old_one() -> None:
    """Regression guard (issue #23): count real listing/homepage fixtures each heuristic misses
    entirely. The old heuristic missed 3 (site02.example, site07.example, site03.example — exactly the URL
    grammars candidates 38/117 exposed); the new one misses only site14.example, a pre-existing,
    genuinely id-less URL grammar no id-based heuristic can close.
    """
    old_zero = []
    new_zero = []
    for path in LISTING_FIXTURES:
        html = path.read_text(errors="replace")
        domain = path.parent.name
        if _old_heuristic(html) == 0:
            old_zero.append(domain)
        if not video_detail_paths(html, f"https://{domain}/"):
            new_zero.append(domain)

    assert sorted(set(old_zero)) == ["site02.example", "site07.example", "site03.example"]
    assert new_zero == ["site14.example"]
    assert len(new_zero) < len(old_zero)


def test_every_fixture_scores_at_least_as_well_as_before_except_the_known_gap() -> None:
    """No real listing fixture the old heuristic already recognised regresses to zero."""
    for path in LISTING_FIXTURES:
        html = path.read_text(errors="replace")
        domain = path.parent.name
        if domain == "site14.example":
            continue
        if _old_heuristic(html) > 0:
            assert video_detail_paths(html, f"https://{domain}/"), (
                f"{path.relative_to(FIXTURES)} regressed to zero"
            )


BASE = "https://example.test/"

# Negative cases (issue #23: "conservative against navigation/category/tag links"). None of these
# domains exist; they are minimal stand-ins for the three shapes the brief named.
_BLOG = """
<html><body>
<a href="/2024/01/15/my-first-post/">My first post</a>
<a href="/2024/02/03/another-update/">Another update</a>
<a href="/2024/03/21/site-news-and-notes/">Site news and notes</a>
<a href="/2024/04/09/behind-the-scenes/">Behind the scenes</a>
<a href="/about/">About</a>
<a href="/contact/">Contact</a>
<a href="/tag/announcements/">Announcements</a>
</body></html>
"""

_LINK_DIRECTORY = """
<html><body>
<a href="https://othersite-one.test/">Other Site One</a>
<a href="https://othersite-two.test/">Other Site Two</a>
<a href="https://othersite-three.test/">Other Site Three</a>
<a href="https://othersite-four.test/">Other Site Four</a>
<a href="/category/video-sites/">Video sites</a>
<a href="/category/stream-sites/">Stream sites</a>
<a href="/about/">About</a>
<a href="/contact/">Contact</a>
</body></html>
"""

_HOSTER_LANDING = """
<html><body>
<a href="/download/482910">Download</a>
<a href="/download/482911">Download</a>
<a href="/download/482912">Download</a>
<a href="/download/482913">Download</a>
<a href="/premium/">Go premium</a>
<a href="/terms/">Terms</a>
</body></html>
"""


def test_blog_scores_zero() -> None:
    assert video_detail_paths(_BLOG, BASE) == frozenset()


def test_link_directory_scores_zero() -> None:
    assert video_detail_paths(_LINK_DIRECTORY, BASE) == frozenset()


def test_hoster_download_links_score_zero() -> None:
    assert video_detail_paths(_HOSTER_LANDING, BASE) == frozenset()


# Positive cases: the URL grammars issue #23 named explicitly.


def test_numeric_id_and_slug() -> None:
    html = '<a href="/video/482910/some-title/">t</a>'
    assert video_detail_paths(html, BASE) == frozenset({"/video/482910/some-title/"})


def test_short_v_path() -> None:
    html = '<a href="/v/482910">t</a>'
    assert video_detail_paths(html, BASE) == frozenset({"/v/482910"})


def test_view_path() -> None:
    html = '<a href="/view/482910">t</a>'
    assert video_detail_paths(html, BASE) == frozenset({"/view/482910"})


def test_embed_path() -> None:
    html = '<a href="/embed/482910">t</a>'
    assert video_detail_paths(html, BASE) == frozenset({"/embed/482910"})


def test_html_detail_page_with_leading_id() -> None:
    html = '<a href="/hdvideo/482910-some_title_here.html">t</a>'
    assert video_detail_paths(html, BASE) == frozenset({"/hdvideo/482910-some_title_here.html"})


def test_language_prefixed_path() -> None:
    html = '<a href="/de/videos/482910/irgendein-titel/">t</a>'
    assert video_detail_paths(html, BASE) == frozenset({"/de/videos/482910/irgendein-titel/"})


def test_id_suffixed_slug_with_short_code() -> None:
    html = '<a href="/some-long-descriptive-title-v30343">t</a>'
    assert video_detail_paths(html, BASE) == frozenset({"/some-long-descriptive-title-v30343"})


def test_bare_id_family_needs_repetition() -> None:
    """A same-shaped family of bare opaque ids counts; one alone does not (weak evidence only)."""
    one = '<a href="/aB3fkPq9wLx7">t</a>'
    three = (
        '<a href="/aB3fkPq9wLx7">t</a><a href="/qZ81mNc4vRt2">t</a><a href="/9WeYd0oGhJk3">t</a>'
    )
    assert video_detail_paths(one, BASE) == frozenset()
    assert len(video_detail_paths(three, BASE)) == 3


def test_pagination_is_not_a_video_id() -> None:
    """A bare short digit run under an unrecognised word (site02's own pagination) never
    counts, even repeated (never anchored, and too short to be trusted as weak evidence)."""
    html = "".join(f'<a href="/hdvideo/{n}">t</a>' for n in range(2, 8))
    assert video_detail_paths(html, BASE) == frozenset()


def test_model_and_tag_links_excluded() -> None:
    html = (
        '<a href="/model/482910/">m</a><a href="/tag/482910/">t</a>'
        '<a href="/category/482910/">c</a><a href="/page/482910/">p</a>'
    )
    assert video_detail_paths(html, BASE) == frozenset()


def test_off_site_links_excluded() -> None:
    html = '<a href="https://elsewhere.test/video/482910/title/">t</a>'
    assert video_detail_paths(html, BASE) == frozenset()
