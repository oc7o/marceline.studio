"""The one URL shaping heuristic, shared by capture refusals (ADR 0016) and the breaker (0041)."""

import pytest

from core.url_shapes import host_of, looks_like_id, path_pattern, url_shape


@pytest.mark.parametrize(
    ("url", "pattern"),
    [
        ("https://h.test/embed/482910", "/embed/*"),
        ("https://h.test/get_file/9f1c2b7a91e0.mp4", "/get_file/*.mp4"),
        ("https://h.test/embed/", "/embed/"),
        ("https://h.test/watch", "/watch"),
        ("https://h.test/", "/"),
        ("https://h.test", "/"),
        # A UUID segment: hyphens keep it out of the alphanumeric-run rule, so it is named
        # separately. Without this a CDN that keys every thumbnail by UUID produces one shape per
        # picture, and a counter that is only ever 1 can protect nothing (ADR 0041 § 1).
        (
            "https://cdn.test/6b5eae5f-59aa-4641-bc30-60d0039a9f4d/1/th_23_p.jpg",
            "/*/*/th_23_p.jpg",
        ),
        # The measured case that has to keep its two shapes apart: a dead poster template and the
        # living hover frames beside it.
        (
            "https://s.test/contents/videos_screenshots/29000/29060/preview.jpg",
            "/contents/videos_screenshots/*/*/preview.jpg",
        ),
        (
            "https://s.test/contents/videos_screenshots/29000/29060/566x302/1.jpg",
            "/contents/videos_screenshots/*/*/566x302/*.jpg",
        ),
        ("https://h.test/a1b2c3", "/*"),  # 6 hex chars
        ("https://h.test/abcxyz", "/abcxyz"),  # not hex, not 12+ chars: kept
        ("https://h.test/sessiontoken12345678", "/*"),  # 12+ char alnum slug with a digit
        # A token or session id lives in the query string, which is dropped outright.
        ("https://h.test/get/1?token=secret#frag", "/get/*"),
        ("https://h.test/thumb.jpg?token=abc123def456", "/thumb.jpg"),
    ],
)
def test_path_pattern_stars_identifier_looking_segments_only(url: str, pattern: str) -> None:
    assert path_pattern(url) == pattern


def test_url_shape_is_the_host_and_the_pattern() -> None:
    assert url_shape("https://WWW.Example.test./thumb/129.jpg") == "www.example.test/thumb/*.jpg"


@pytest.mark.parametrize(
    "segment", ["482910", "9f1c2b7a91e0", "a1b2c3d4e5f6g7", "6b5eae5f-59aa-4641-bc30-60d0039a9f4d"]
)
def test_identifiers(segment: str) -> None:
    assert looks_like_id(segment)


@pytest.mark.parametrize("segment", ["", "preview", "566x302", "videos_screenshots", "th_23_p"])
def test_not_identifiers(segment: str) -> None:
    assert not looks_like_id(segment)


def test_host_of_lowercases_and_drops_the_trailing_dot() -> None:
    assert host_of("https://CDN.Example.test./a") == "cdn.example.test"
