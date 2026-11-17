"""The draft can see a picture-set post, not only a video page (issue #118, ADR 0043).

Every fixture here is HTML written for this test — no recorded site markup, no media, no request
(root `CLAUDE.md`). An independent creator's paid posts are republished as sets of stills far more
often than as a video page (`docs/research/creator-leak-sources.md`), and an onboarding draft that
calls such a page `unknown` leaves the adapter author with nothing.
"""

from selectolax.lexbor import LexborHTMLParser

from crawler.drafting import (
    GALLERY,
    UNKNOWN,
    _anchors,
    analyse,
    classify,
    gallery_family,
    render,
)

DOMAIN = "gallery.example"
BASE = f"https://{DOMAIN}/"


def _html(body: str, head: str = "") -> str:
    return f"<html><head><title>A post</title>{head}</head><body>{body}</body></html>"


def _post(count: int, attribute: str = "src") -> str:
    pictures = "".join(
        f'<a href="/full/set-41/{index}.jpg"><img {attribute}="/thumb/set-41/{index}.jpg"></a>'
        for index in range(count)
    )
    return _html(f'<div class="post">{pictures}</div><img src="/static/logo.png">')


def _kind(html: str) -> str:
    tree = LexborHTMLParser(html)
    return classify(tree, _anchors(tree, BASE))[0]


def test_a_post_of_same_shaped_pictures_is_a_gallery() -> None:
    assert _kind(_post(12)) == GALLERY


def test_a_lazy_loaded_gallery_is_seen_through_its_data_attribute() -> None:
    assert _kind(_post(9, attribute="data-src")) == GALLERY


def test_json_ld_says_it_outright() -> None:
    head = '<script type="application/ld+json">{"@type": "ImageGallery"}</script>'
    assert _kind(_html("<p>a post</p>", head=head)) == GALLERY


def test_a_page_with_a_poster_and_a_theme_is_not_a_gallery() -> None:
    body = '<img src="/poster/9.jpg"><img src="/static/logo.png"><img src="/static/avatar.png">'
    body += "<p>" + ("some ordinary page text. " * 40) + "</p>"
    assert _kind(_html(body)) == UNKNOWN


def test_the_biggest_family_is_reported_with_its_shape_and_attribute() -> None:
    template, count, attribute = gallery_family(LexborHTMLParser(_post(12)))
    assert (count, attribute) == (12, "src")
    assert template.startswith("/thumb/")


def test_the_draft_names_the_photo_media_kind_and_prints_no_value() -> None:
    draft = analyse(DOMAIN, {"post.html": _post(12)})
    output = "\n".join(render(draft))

    assert any(page.kind == GALLERY for page in draft.pages)
    assert "Media.Kind.PHOTO" in output
    assert "pictures   12x @src" in output
    # Structure only: no image URL and no title of the page reaches the draft (ADR 0043).
    assert "/thumb/set-41/0.jpg" not in output
    assert "A post" not in output
