"""Redirects are checked before they are followed (crawler review H4, ADR 0007).

The site allowlist and robots.txt are crawler policy, so they hold for every request — including
the one a redirect asks for. Checking only the final URL afterwards means the off-site or
disallowed page has already been requested. These tests assert on the transport.
"""

import httpx
import pytest

from catalog.models import Site
from core.http import PoliteClient
from crawler import services
from crawler.models import CrawlTask
from crawler.sites.base import TaskSpec
from crawler.tests.helpers import load_fixture
from crawler.worker import CrawlWorker

UA = "MarcelineBot/0.1"
ROBOTS_URL = "https://site01.example/robots.txt"
VIDEO_URL = "https://site01.example/video/2099/lena-hartwell-new-video-part-1/"

Route = tuple[int, str, dict[str, str]]


class Upstream:
    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        status, body, headers = self.routes.get(url, (404, "not found", {}))
        return httpx.Response(status, text=body, headers={"content-type": "text/html", **headers})

    def run(self) -> CrawlTask:
        client = PoliteClient(
            UA, max_retries=0, transport=httpx.MockTransport(self.handler), sleep=lambda _: None
        )
        CrawlWorker(client, UA).run(once=True)
        return CrawlTask.objects.get(url=VIDEO_URL)


@pytest.fixture
def site01(db: None) -> Site:
    site = Site.objects.create(domain="site01.example", crawl_delay_s=0)
    services.enqueue(site, [TaskSpec(VIDEO_URL, CrawlTask.Kind.DETAIL)])
    return site


def redirect(location: str) -> Route:
    return (302, "", {"location": location})


@pytest.mark.usefixtures("site01")
def test_an_off_site_redirect_is_never_requested() -> None:
    upstream = Upstream(
        {
            ROBOTS_URL: (200, "", {}),
            VIDEO_URL: redirect("https://not-allowlisted.test/landing"),
            "https://not-allowlisted.test/landing": (200, "<html></html>", {}),
        }
    )

    task = upstream.run()

    assert task.status == CrawlTask.Status.SKIPPED
    assert "off-site" in task.decision_reason
    assert upstream.requested == [ROBOTS_URL, VIDEO_URL]


@pytest.mark.usefixtures("site01")
def test_a_redirect_to_a_path_robots_disallows_is_never_requested() -> None:
    upstream = Upstream(
        {
            ROBOTS_URL: (200, "User-agent: *\nDisallow: /private/\n", {}),
            VIDEO_URL: redirect("/private/video"),
            "https://site01.example/private/video": (200, "<html></html>", {}),
        }
    )

    task = upstream.run()

    assert task.status == CrawlTask.Status.SKIPPED
    assert "robots.txt" in task.decision_reason
    assert upstream.requested == [ROBOTS_URL, VIDEO_URL]


@pytest.mark.usefixtures("site01")
def test_a_redirect_to_another_host_of_the_site_reads_that_hosts_robots_first() -> None:
    www_video = VIDEO_URL.replace("https://site01.example", "https://www.site01.example")
    upstream = Upstream(
        {
            ROBOTS_URL: (200, "", {}),
            VIDEO_URL: redirect(www_video),
            "https://www.site01.example/robots.txt": (200, "", {}),
            www_video: (200, load_fixture("site01.example", "video-2.html"), {}),
        }
    )

    task = upstream.run()

    assert task.status == CrawlTask.Status.DONE
    assert upstream.requested == [
        ROBOTS_URL,
        VIDEO_URL,
        "https://www.site01.example/robots.txt",
        www_video,
    ]
