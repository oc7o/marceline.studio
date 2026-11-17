"""Request counters, host roles and budgets (issue #74, ADR 0007 amendment 2026-09-17)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
from django.db import DatabaseError

from catalog import request_accounting as accounting
from catalog.models import RequestCount, RequestCountDaily, Site, SiteHost
from catalog.request_accounting import RequestAccounting
from catalog.sources import CDN_HOST_DAILY_REQUEST_BUDGET
from core.http import BudgetExhaustedError, PoliteClient, RequestAttempt, RequestLabel

NOW = datetime(2026, 9, 17, 10, 30, tzinfo=UTC)
HOUR = NOW.replace(minute=0)


class Clock:
    def __init__(self) -> None:
        self.mono = 0.0
        self.now = NOW

    def monotonic(self) -> float:
        return self.mono

    def wall(self) -> datetime:
        return self.now


@pytest.fixture
def site(db: None) -> Site:
    return Site.objects.create(
        domain="site.test", daily_request_budget=3, daily_media_request_budget=2
    )


def attempt(
    url: str, site: Site | None = None, kind: str = "page", *, enforce: bool = True, role: str = ""
) -> RequestAttempt:
    label = RequestLabel(kind, site_id=site.pk if site else None, role=role, enforce=enforce)
    return RequestAttempt(url, accounting.host_of(url), label)


def make(clock: Clock) -> RequestAccounting:
    return RequestAccounting(clock=clock.monotonic, now=clock.wall)


def add(
    site: Site | None,
    host: str,
    role: str,
    kind: str,
    count: int,
    *,
    hour: datetime = HOUR,
    request_type: str = "",
    outcome: str = "2xx",
    size: int = 0,
) -> None:
    """Counters as another process flushed them."""
    key = (hour, site.pk if site else None, host, role, kind, request_type or kind, outcome)
    accounting.upsert_counts({key: [count, size]})


def counts() -> list[tuple[object, ...]]:
    return list(
        RequestCount.objects.order_by("host", "request_type", "outcome").values_list(
            "site_id", "host", "role", "kind", "request_type", "outcome", "requests", "bytes"
        )
    )


def test_counters_are_aggregated_per_hour_and_dimension_and_added_up(site: Site) -> None:
    recorder = make(Clock())
    recorder.record(attempt("https://site.test/a", site), "2xx", 100)
    recorder.record(attempt("https://www.site.test/b", site), "2xx", 50)
    recorder.record(attempt("https://site.test/c", site), "2xx", 10)
    retry = RequestAttempt("https://site.test/c", "site.test", RequestLabel("page", site.pk), 0, 2)
    recorder.record(retry, "5xx", 0)
    recorder.record(attempt("https://cdn.example/x.jpg", site, "media"), "4xx", 0)
    recorder.record(attempt("https://rdap.example/ip/1", None, "rdap"), "2xx", 7)
    recorder.flush()
    recorder.record(attempt("https://rdap.example/ip/2", None, "rdap"), "2xx", 3)
    recorder.flush()

    assert counts() == [
        (site.pk, "cdn.example", "cdn", "media", "media", "4xx", 1, 0),
        (None, "rdap.example", "third_party", "rdap", "rdap", "2xx", 2, 10),
        (site.pk, "site.test", "origin", "page", "page", "2xx", 2, 110),
        (site.pk, "site.test", "origin", "page", "retry", "5xx", 1, 0),
        (site.pk, "www.site.test", "origin", "page", "page", "2xx", 1, 50),
    ]
    assert RequestCount.objects.get(host="www.site.test").hour == HOUR


def test_buffer_flushes_by_itself_after_the_interval(site: Site) -> None:
    clock = Clock()
    recorder = make(clock)
    recorder.record(attempt("https://site.test/a", site), "2xx", 1)
    assert not RequestCount.objects.exists()
    clock.mono += accounting.FLUSH_INTERVAL_S
    recorder.record(attempt("https://site.test/b", site), "2xx", 1)
    assert RequestCount.objects.get().requests == 2


def test_a_failed_flush_keeps_the_counts_for_the_next_one(site: Site) -> None:
    recorder = make(Clock())
    recorder.record(attempt("https://site.test/a", site), "2xx", 1)
    with patch.object(accounting, "upsert_counts", side_effect=DatabaseError("down")):
        recorder.flush()
    recorder.flush()
    assert RequestCount.objects.get().requests == 1


def test_site_hosts_override_the_role_rule(site: Site) -> None:
    SiteHost.objects.create(site=site, host="img.site.test", role="cdn", source="operator")
    SiteHost.objects.create(site=site, host="mirror.example", role="origin", source="operator")
    roles = accounting.HostRoles()
    label = RequestLabel("media", site_id=site.pk)
    assert roles.role(label, "site.test") == "origin"
    assert roles.role(label, "img.site.test") == "cdn"
    assert roles.role(label, "mirror.example") == "origin"
    assert roles.role(label, "other.example") == "cdn"
    assert roles.role(RequestLabel("rdap"), "rdap.example") == "third_party"
    assert roles.role(RequestLabel("probe", role="origin"), "candidate.example") == "origin"


def test_origin_page_budget_counts_every_process_and_frees_up_as_hours_leave_the_window(
    site: Site,
) -> None:
    add(site, "site.test", "origin", "page", 1)
    add(site, "site.test", "origin", "page", 1, request_type="retry", outcome="5xx")
    add(site, "site.test", "origin", "page", 5, outcome="refused")  # never sent
    add(site, "site.test", "origin", "media", 50)  # the media budget's business
    add(site, "cdn.example", "cdn", "media", 50)  # the CDN's business
    add(site, "site.test", "origin", "page", 9, hour=HOUR - timedelta(hours=24))  # left
    clock = Clock()
    recorder = make(clock)
    robots = attempt("https://site.test/robots.txt", site, "robots")
    recorder.admit(robots)  # 2 of 3 used
    recorder.record(robots, "2xx", 0)
    with pytest.raises(BudgetExhaustedError) as refused:
        recorder.admit(attempt("https://site.test/p", site))
    assert refused.value.scope == "origin:page"
    assert refused.value.resume_at == HOUR + timedelta(hours=1)
    assert accounting.over_budget_site_ids(NOW) == [site.pk]  # the refusal flushed its count

    # 23 hours later these requests are still in the window, an hour later they have left it.
    clock.now = NOW + timedelta(hours=23)
    with pytest.raises(BudgetExhaustedError):
        recorder.admit(robots)
    clock.now = NOW + timedelta(hours=24)
    recorder.admit(robots)
    assert accounting.over_budget_site_ids(clock.now) == []


def test_origin_media_has_its_own_budget_and_media_on_a_cdn_never_touches_it(site: Site) -> None:
    add(site, "site.test", "origin", "page", 3)  # page budget used up
    recorder = make(Clock())
    origin_media = attempt("https://www.site.test/poster.jpg", site, "media")
    recorder.admit(origin_media)
    recorder.record(origin_media, "2xx", 0)
    recorder.record(attempt("https://cdn.example/a.jpg", site, "media"), "2xx", 0)
    recorder.record(attempt("https://cdn.example/b.jpg", site, "media"), "2xx", 0)
    recorder.admit(origin_media)  # 1 of 2: the two CDN requests do not count
    recorder.record(origin_media, "2xx", 0)
    with pytest.raises(BudgetExhaustedError) as refused:
        recorder.admit(origin_media)
    assert (refused.value.scope, refused.value.host) == ("origin:media", "www.site.test")
    recorder.admit(attempt("https://cdn.example/c.jpg", site, "media"))
    assert accounting.over_budget_site_ids(NOW, budget=accounting.MEDIA_BUDGET) == [site.pk]


def test_budget_reads_are_cached_and_local_requests_are_added_in_between(site: Site) -> None:
    site.daily_request_budget = 2
    site.save()
    clock = Clock()
    recorder = make(clock)
    page = attempt("https://site.test/p", site)
    recorder.admit(page)
    # Another process uses the whole budget and flushes; the cached read cannot see that yet.
    add(site, "site.test", "origin", "page", 2)
    recorder.record(page, "2xx", 0)
    recorder.admit(page)  # 1 of 2 as far as this process knows
    recorder.record(page, "2xx", 0)
    with pytest.raises(BudgetExhaustedError):
        recorder.admit(page)  # its own two requests are enough
    clock.mono += accounting.USAGE_CACHE_S
    with pytest.raises(BudgetExhaustedError) as refused:
        recorder.admit(page)
    assert "4/2" in str(refused.value)  # re-read after flushing its own buffer


def test_cdn_budget_is_per_host_across_sites_with_the_lowest_override(site: Site) -> None:
    other = Site.objects.create(domain="other.test")
    add(site, "shared.cdn", "cdn", "media", 4)
    add(other, "shared.cdn", "cdn", "media", 5)
    assert accounting.cdn_host_budget("shared.cdn") == CDN_HOST_DAILY_REQUEST_BUDGET
    SiteHost.objects.create(site=site, host="shared.cdn", role="cdn", daily_request_budget=20)
    SiteHost.objects.create(site=other, host="shared.cdn", role="cdn", daily_request_budget=10)
    assert accounting.cdn_host_budget("shared.cdn") == 10

    recorder = make(Clock())
    media = attempt("https://shared.cdn/a.jpg", other, "media")
    recorder.admit(media)  # 9 of 10
    recorder.record(media, "2xx", 0)
    with pytest.raises(BudgetExhaustedError) as refused:
        recorder.admit(attempt("https://shared.cdn/b.jpg", site, "media"))
    assert (refused.value.scope, refused.value.host) == ("cdn", "shared.cdn")
    recorder.admit(attempt("https://site.test/p", site))  # origin budgets are separate


def test_unbudgeted_kinds_unenforced_labels_and_siteless_traffic_are_never_refused(
    site: Site,
) -> None:
    Site.objects.filter(pk=site.pk).update(daily_request_budget=0, daily_media_request_budget=0)
    recorder = make(Clock())
    recorder.admit(attempt("https://site.test/img.jpg", site, "review_image", enforce=False))
    recorder.admit(attempt("https://site.test/", site, "probe"))
    recorder.admit(attempt("https://rdap.example/ip/1", None, "rdap"))
    with pytest.raises(BudgetExhaustedError):
        recorder.admit(attempt("https://site.test/p", site))


def test_polite_client_counts_retries_redirects_and_refusals_end_to_end(site: Site) -> None:
    served: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        served.append(request.url.path)
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        if served.count("/new") == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=b"ok")

    client = PoliteClient(
        "MarcelineBot/0.1",
        transport=httpx.MockTransport(handler),
        sleep=lambda seconds: None,
        observer=RequestAccounting(),
    )
    label = RequestLabel("page", site_id=site.pk)
    client.get("https://site.test/old", label=label)  # three requests: the budget
    with pytest.raises(BudgetExhaustedError):
        client.get("https://site.test/next", label=label)
    client.close()

    assert served == ["/old", "/new", "/new"]
    assert {(row[3], row[4], row[5], row[6]) for row in counts()} == {
        ("page", "page", "3xx", 1),
        ("page", "redirect_hop", "5xx", 1),
        ("page", "retry", "2xx", 1),
        ("page", "page", "refused", 1),
    }


def test_rollup_moves_old_hours_into_days_once(site: Site) -> None:
    old = datetime(2026, 8, 1, 5, tzinfo=UTC)
    add(site, "site.test", "origin", "page", 3, hour=old, size=30)
    add(site, "site.test", "origin", "page", 4, hour=old + timedelta(hours=2), size=40)
    add(None, "rdap.example", "third_party", "rdap", 1, hour=old, size=1)
    add(site, "site.test", "origin", "page", 1, size=1)

    assert accounting.rollup_request_counts(NOW) == 3
    assert accounting.rollup_request_counts(NOW) == 0
    assert RequestCount.objects.count() == 1
    assert list(
        RequestCountDaily.objects.order_by("host").values_list(
            "site_id", "host", "kind", "day", "requests", "bytes"
        )
    ) == [
        (None, "rdap.example", "rdap", old.date(), 1, 1),
        (site.pk, "site.test", "page", old.date(), 7, 70),
    ]
    rows = accounting.usage_rows(old - timedelta(days=1), site_ids=[site.pk])
    assert [(row.host, row.requests) for row in rows] == [("site.test", 8)]


def test_observed_hosts_are_recorded_once_with_the_rule_and_never_overwrite(site: Site) -> None:
    SiteHost.objects.create(site=site, host="img.site.test", role="cdn", source="operator")
    created = accounting.record_observed_hosts(
        [
            (site.pk, "site.test"),
            (site.pk, "img.site.test"),
            (site.pk, "Thumbs.CDN.example"),
            (site.pk, "thumbs.cdn.example"),
            (site.pk, "bad host"),
        ]
    )
    assert created == 2
    assert sorted(SiteHost.objects.values_list("host", "role", "source")) == [
        ("img.site.test", "cdn", "operator"),
        ("site.test", "origin", "observed"),
        ("thumbs.cdn.example", "cdn", "observed"),
    ]
