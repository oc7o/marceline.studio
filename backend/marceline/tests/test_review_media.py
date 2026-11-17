from collections.abc import Callable

import httpx
import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone

from catalog.models import Site
from core.http import PoliteClient
from index.models import Media, Page
from marceline.review import media as media_api

Handler = Callable[[httpx.Request], httpx.Response]
JPEG = b"\xff\xd8\xff\xe0 synthetic bytes"


class Recorded(list[httpx.Request]):
    """Upstream image requests; robots.txt requests are kept apart in `robots`."""

    def __init__(self) -> None:
        super().__init__()
        self.robots: list[httpx.Request] = []


def upstream(
    monkeypatch: pytest.MonkeyPatch, handler: Handler, robots: Handler | None = None
) -> Recorded:
    """Route the passthrough client to a mock transport; record every upstream request.

    robots.txt answers 404 (everything allowed) unless `robots` says otherwise."""
    requests = Recorded()

    def record(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            requests.robots.append(request)
            return robots(request) if robots is not None else httpx.Response(404)
        requests.append(request)
        return handler(request)

    def build() -> PoliteClient:
        return PoliteClient(
            settings.CRAWLER_USER_AGENT,
            transport=httpx.MockTransport(record),
            max_bytes=media_api.MAX_IMAGE_BYTES,
            max_retries=0,
            sleep=lambda _: None,
            public_only=True,
            resolver=lambda host: list(DNS.get(host, [])),
        )

    monkeypatch.setattr(media_api, "build_client", build)
    return requests


# What the fake resolver answers. Everything else does not resolve.
DNS = {
    "cdn.example.test": ["93.184.216.34"],
    "other-cdn.example.test": ["2606:4700::1"],
    "rebound.example.test": ["10.0.0.7"],
}


def image(content: bytes = JPEG, content_type: str = "image/jpeg") -> Handler:
    headers = {"content-type": content_type}
    return lambda request: httpx.Response(200, content=content, headers=headers)


def make_media(
    site: Site,
    kind: str = "poster",
    safety_status: str = "clear",
    safety_score: float | None = 1.0,
    *,
    eligible: bool = True,
) -> Media:
    page = Page.objects.create(
        site=site,
        url=f"https://example.test/{kind}-{safety_status}-{safety_score}-{eligible}",
        safety_status=safety_status,
        safety_score=safety_score,
    )
    return Media.objects.create(
        page=page,
        kind=kind,
        url="https://cdn.example.test/image.jpg",
        eligible_at=timezone.now() if eligible else None,
    )


def url(media: Media | int) -> str:
    return f"/api/v1/media/{media if isinstance(media, int) else media.pk}/image"


def test_happy_path_streams_bytes_without_caching(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = upstream(monkeypatch, image(content_type="image/jpeg; charset=binary"))
    row = make_media(site)

    response = staff_client.get(url(row))

    assert response.status_code == 200
    assert response.content == JPEG
    assert response["Content-Type"] == "image/jpeg"
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    [request] = requests
    assert str(request.url) == "https://cdn.example.test/image.jpg"
    assert request.headers["user-agent"] == settings.CRAWLER_USER_AGENT
    row.refresh_from_db()
    assert row.byte_size is None  # nothing stored


@pytest.mark.parametrize("status", ["review", "blocked"])
def test_flagged_pages_are_forbidden(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    requests = upstream(monkeypatch, image())

    response = staff_client.get(url(make_media(site, safety_status=status)))

    assert response.status_code == 403
    assert requests == [] and requests.robots == []


@pytest.mark.parametrize(
    ("safety_score", "eligible"),
    [(None, True), (0.5, True), (1.0, False)],
    ids=["never-scored", "below-threshold", "not-eligible"],
)
def test_gated_pages_are_forbidden_and_fetch_nothing(
    staff_client: Client,
    site: Site,
    monkeypatch: pytest.MonkeyPatch,
    safety_score: float | None,
    eligible: bool,
) -> None:
    """The passthrough is the second media fetch path; the gate covers it too (issue #65)."""
    requests = upstream(monkeypatch, image())

    response = staff_client.get(url(make_media(site, safety_score=safety_score, eligible=eligible)))

    assert response.status_code == 403
    assert requests == [] and requests.robots == []


@pytest.mark.parametrize("content_type", ["text/html", "image/svg+xml", ""])
def test_non_image_content_is_rejected(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch, content_type: str
) -> None:
    upstream(monkeypatch, image(b"<html></html>", content_type))

    response = staff_client.get(url(make_media(site)))

    assert response.status_code == 502
    assert response.json() == {
        "detail": "image unavailable",
        "reason": "upstream did not return an image",
    }


def test_size_cap(staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    upstream(monkeypatch, image(b"x" * (media_api.MAX_IMAGE_BYTES + 1)))

    response = staff_client.get(url(make_media(site)))

    assert response.status_code == 502
    assert response.json()["reason"] == "larger than 5 MB"


@pytest.mark.parametrize("kind", ["preview", "sprite"])
def test_only_still_image_media_is_served(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    requests = upstream(monkeypatch, image())

    assert staff_client.get(url(make_media(site, kind=kind))).status_code == 404
    assert staff_client.get(url(999999)).status_code == 404
    assert requests == []


@pytest.mark.parametrize("kind", ["frame", "photo"])
def test_frame_and_photo_media_are_served(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A photo post's image is as viewable in review as a poster or a hover frame (issue #69)."""
    upstream(monkeypatch, image(content_type="image/webp"))

    assert staff_client.get(url(make_media(site, kind=kind))).status_code == 200


def test_expired_upstream_is_404(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream(monkeypatch, lambda request: httpx.Response(404))

    response = staff_client.get(url(make_media(site)))

    assert response.status_code == 404
    assert response.json() == {"detail": "image unavailable", "reason": "expired or removed"}


@pytest.mark.parametrize("status", [301, 400, 418, 500, 503])
def test_upstream_status_codes_are_not_passed_on(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A reviewer learns that the image is unavailable, not how the upstream server answered."""
    upstream(monkeypatch, lambda request: httpx.Response(status))

    response = staff_client.get(url(make_media(site)))

    assert response.status_code == 502
    assert response.json() == {"detail": "image unavailable", "reason": "upstream unavailable"}
    assert str(status) not in response.content.decode()


def test_upstream_network_errors_are_generic(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused by 203.0.113.9", request=request)

    upstream(monkeypatch, refuse)

    response = staff_client.get(url(make_media(site)))

    assert response.status_code == 502
    assert response.json() == {"detail": "image unavailable", "reason": "upstream unavailable"}


@pytest.mark.parametrize(
    "media_url",
    [
        "http://127.0.0.1:8000/admin/",
        "http://[::1]:8000/admin/",
        "http://169.254.169.254/latest/meta-data/",
        "http://rebound.example.test/image.jpg",
        "http://unresolvable.example.test/image.jpg",
        "file:///etc/passwd",
    ],
)
def test_private_hosts_are_refused(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch, media_url: str
) -> None:
    requests = upstream(monkeypatch, image())
    row = make_media(site)
    row.url = media_url
    row.save()

    response = staff_client.get(url(row))

    assert response.status_code == 403
    assert response.json() == {"detail": "image unavailable", "reason": "media host not allowed"}
    assert requests == [] and requests.robots == []


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1:8000/api/v1/review/overview",
        "http://10.0.0.2/",
        "http://[fd00::1]/",
        "http://rebound.example.test/image.jpg",
        "/../../../../",  # same host: allowed through to the next check, then 404s here
    ],
)
def test_every_redirect_hop_is_checked_before_it_is_followed(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example.test" and request.url.path == "/image.jpg":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(404)

    requests = upstream(monkeypatch, handler)

    response = staff_client.get(url(make_media(site)))

    if location.startswith("/"):
        assert response.status_code == 404
        assert len(requests) == 2
        return
    assert response.status_code == 403
    assert response.json() == {"detail": "image unavailable", "reason": "media host not allowed"}
    assert [str(request.url) for request in requests] == ["https://cdn.example.test/image.jpg"]


def test_a_redirect_to_another_public_cdn_is_followed(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example.test":
            return httpx.Response(302, headers={"location": "https://other-cdn.example.test/i.jpg"})
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    requests = upstream(monkeypatch, handler)

    response = staff_client.get(url(make_media(site)))

    assert response.status_code == 200
    assert [request.url.host for request in requests] == [
        "cdn.example.test",
        "other-cdn.example.test",
    ]


def test_the_real_client_connects_only_to_checked_addresses() -> None:
    with media_api.build_client() as client:
        assert client.public_only


def test_rate_limit(staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    upstream(monkeypatch, image())
    monkeypatch.setattr(media_api, "limiter", media_api.RateLimiter(2, 1.0, clock=lambda: 0.0))
    row = make_media(site)

    statuses = [staff_client.get(url(row)).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]


def test_rate_limiter_window() -> None:
    now = [0.0]
    limiter = media_api.RateLimiter(1, 1.0, clock=lambda: now[0])
    assert limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")
    now[0] = 1.0
    assert limiter.allow("a")


# --- the media host's own robots.txt (ADR 0007 amendment 2026-09-17) --------------------------


def robots_txt(body: bytes) -> Handler:
    return lambda request: httpx.Response(200, content=body, headers={"content-type": "text/plain"})


def test_a_media_host_disallowing_us_is_refused_and_its_image_never_requested(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = upstream(monkeypatch, image(), robots_txt(b"User-agent: *\nDisallow: /\n"))
    row = make_media(site)

    first = staff_client.get(url(row))
    second = staff_client.get(url(row))

    for response in (first, second):
        assert response.status_code == 403
        assert response.json() == {
            "detail": "image unavailable",
            "reason": "media host disallows us in robots.txt",
        }
    assert requests == []
    # Cached per web process: the second request asks nobody.
    assert [str(request.url) for request in requests.robots] == [
        "https://cdn.example.test/robots.txt"
    ]


def test_a_redirect_to_a_disallowing_host_is_refused_before_it_is_followed(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://other-cdn.example.test/i.jpg"})

    def robots(request: httpx.Request) -> httpx.Response:
        if request.url.host == "other-cdn.example.test":
            return robots_txt(b"User-agent: MarcelineBot\nDisallow: /i.jpg\n")(request)
        return httpx.Response(404)

    requests = upstream(monkeypatch, handler, robots)

    response = staff_client.get(url(make_media(site)))

    assert response.status_code == 403
    assert [request.url.host for request in requests] == ["cdn.example.test"]
    assert [request.url.host for request in requests.robots] == [
        "cdn.example.test",
        "other-cdn.example.test",
    ]


def test_an_unreadable_robots_txt_is_a_generic_upstream_error(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = upstream(monkeypatch, image(), lambda request: httpx.Response(503))

    response = staff_client.get(url(make_media(site)))

    assert response.status_code == 502
    assert response.json() == {"detail": "image unavailable", "reason": "upstream unavailable"}
    assert requests == []


def test_robots_requests_are_counted_for_the_site(
    staff_client: Client, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalog.models import RequestCount
    from catalog.request_accounting import RequestAccounting

    upstream(monkeypatch, image())
    build = media_api.build_client

    def accounted() -> PoliteClient:
        client = build()
        client._observer = RequestAccounting()
        return client

    monkeypatch.setattr(media_api, "build_client", accounted)

    assert staff_client.get(url(make_media(site))).status_code == 200

    assert sorted(RequestCount.objects.values_list("site_id", "kind", "outcome")) == [
        (site.pk, "review_image", "2xx"),
        (site.pk, "robots", "4xx"),
    ]
