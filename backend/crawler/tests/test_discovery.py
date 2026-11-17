import json
from collections.abc import Callable
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from django.contrib.admin.sites import AdminSite
from django.core.management import CommandError, call_command
from django.test import RequestFactory, override_settings

from catalog.models import Creator, Site
from crawler.admin import SearchCallAdmin, SourceCandidateAdmin
from crawler.discovery.dataforseo import DataForSEO, DiscoveryError, ProviderError, SpendLimit
from crawler.discovery.domains import classify, normalize_domain
from crawler.discovery.probe import probe
from crawler.discovery.services import EVIDENCE_LIMIT, Query, apply_rules, mine_links, upsert
from crawler.models import SearchCall, SourceCandidate
from index.models import Page

pytestmark = pytest.mark.django_db
CREDS = override_settings(DATAFORSEO_LOGIN="secret-login", DATAFORSEO_PASSWORD="secret-pass")
FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def response_data(cost: str = "0.002") -> dict[str, Any]:
    return {
        "status_code": 20000,
        "cost": cost,
        "tasks": [
            {
                "status_code": 20000,
                "id": "task-1",
                "result": [
                    {
                        "items": [
                            {
                                "type": "organic",
                                "rank_absolute": 2,
                                "url": "https://WWW.Newsite.test/v",
                                "domain": "www.newsite.test",
                                "title": "Result",
                            },
                            {"type": "paid", "rank_absolute": 1, "url": "https://ad.test/"},
                        ]
                    }
                ],
            }
        ],
    }


@CREDS
def test_live_and_actual_cost() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/live/regular")
        assert json.loads(request.content)[0]["depth"] == 10
        return httpx.Response(200, json=response_data("0.0019"))

    client = DataForSEO(transport=httpx.MockTransport(handler))
    assert client.search("name leaked")[0]["rank"] == 2
    log = SearchCall.objects.get()
    assert log.cost_usd == Decimal("0.0019")
    assert log.result_count == 1
    assert log.status == "ok"


@CREDS
def test_standard() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/task_post"):
            data = {
                "status_code": 20000,
                "cost": 0.0006,
                "tasks": [{"status_code": 20100, "id": "task-1"}],
            }
        elif request.url.path.endswith("/tasks_ready"):
            data = {
                "status_code": 20000,
                "cost": 0,
                "tasks": [{"status_code": 20000, "result": [{"id": "task-1"}]}],
            }
        else:
            data = response_data("0")
        return httpx.Response(200, json=data)

    client = DataForSEO("standard", transport=httpx.MockTransport(handler))
    assert len(client.search("query")) == 1
    assert len(paths) == 3
    assert paths[-1].endswith("/task_get/regular/task-1")
    assert SearchCall.objects.count() == 3
    assert client.cost == Decimal("0.0006")


@CREDS
@pytest.mark.parametrize("http_status,code", [(200, 40501), (500, 50000)])
def test_error_logged(http_status: int, code: int) -> None:
    data = response_data("0.0001")
    data["status_code"] = code
    with pytest.raises(DiscoveryError):
        DataForSEO(
            transport=httpx.MockTransport(lambda request: httpx.Response(http_status, json=data))
        ).search("query")
    log = SearchCall.objects.get()
    assert log.status == "error"
    assert log.cost_usd == Decimal("0.0001")
    assert "secret" not in log.error


@CREDS
def test_transport_error_logged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-login", request=request)

    with pytest.raises(DiscoveryError):
        DataForSEO(transport=httpx.MockTransport(handler)).search("query")
    assert SearchCall.objects.get().error == "ConnectError"


@CREDS
@override_settings(DISCOVERY_DAILY_BUDGET_USD="0.003")
def test_budget_and_run_cap() -> None:
    client = DataForSEO(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response_data()))
    )
    client.search("one")
    with pytest.raises(SpendLimit):
        client.search("two")
    assert SearchCall.objects.count() == 1
    with pytest.raises(SpendLimit):
        DataForSEO(max_calls=0).search("zero")
    assert SearchCall.objects.count() == 1


@override_settings(DATAFORSEO_LOGIN="", DATAFORSEO_PASSWORD="")
def test_dry_run_no_calls() -> None:
    Creator.objects.create(name="Example One", aliases=["Alias"])
    Creator.objects.create(name="Example Two")
    out = StringIO()
    with patch("httpx.Client.request", side_effect=AssertionError("network")):
        call_command("discover_sources", dry_run=True, stdout=out)
    assert "Example One leaked" in out.getvalue()
    assert "Alias fanhub leak" in out.getvalue()
    assert "queries=9 estimated cost=$0.018000" in out.getvalue()
    assert not SearchCall.objects.exists()
    assert not SourceCandidate.objects.exists()
    with pytest.raises(DiscoveryError, match="DATAFORSEO_LOGIN"):
        DataForSEO()


@pytest.mark.parametrize(
    "value,domain",
    [
        ("https://WWW.Sub.Example.COM/a", "example.com"),
        ("www.foo.example.co.uk", "example.co.uk"),
        ("https://127.0.0.1/", ""),
        ("javascript:alert(1)", ""),
    ],
)
def test_normalize(value: str, domain: str) -> None:
    assert normalize_domain(value) == domain


def test_upsert_dedupe_and_review_preserved() -> None:
    creator = Creator.objects.create(name="Example")
    candidate, created = upsert("https://www.New.test/a", query=Query("one", creator.pk), rank=3)
    assert candidate and created
    candidate.status = "rejected"
    candidate.save()
    for i in range(EVIDENCE_LIMIT + 2):
        upsert("https://sub.NEW.test/b", query=Query(str(i), creator.pk), rank=2)
    candidate.refresh_from_db()
    assert SourceCandidate.objects.count() == 1
    assert candidate.distinct_creators == 1
    assert candidate.best_rank == 2
    assert candidate.status == "rejected"
    assert len(candidate.evidence["queries"]) == EVIDENCE_LIMIT


@pytest.mark.parametrize(
    "domain", ["fanhub.example", "www.google.co.uk", "reddit.com", "sub.existing.test"]
)
def test_exclusions(domain: str) -> None:
    Site.objects.create(domain="existing.test")
    assert upsert(domain) == (None, False)


@pytest.mark.parametrize(
    "domain,kind,status",
    [
        ("luluvdo.com", "file_hoster", "hoster"),
        ("send.cm", "file_hoster", "hoster"),
        ("cuty.io", "file_hoster", "hoster"),
        ("t.me", "social", "ignored"),
        ("p2p-site.example", "torrent", "torrent"),
        ("new.test", "unknown", "proposed"),
    ],
)
def test_classification(domain: str, kind: str, status: str) -> None:
    assert classify(domain) == (kind, status)
    candidate, _ = upsert(domain)
    assert candidate and candidate.kind == kind and candidate.status == status


def test_from_links(make_page: Callable[[str], Page]) -> None:
    page = make_page("one")
    page.raw_extraction = {
        "external_links": [
            "https://www.new.test/a",
            "https://sub.new.test/b",
            "https://luluvdo.com/a",
            "https://fanhub.example/x",
        ]
    }
    page.save()
    out = StringIO()
    call_command("discover_sources", from_links=True, stdout=out)
    assert "new=2" in out.getvalue()
    assert mine_links() == (0, 2)
    assert SourceCandidate.objects.get(domain="new.test").outlink_pages == 1
    assert not SearchCall.objects.exists()


@pytest.mark.parametrize(
    "fixture,status,reason,kind",
    [
        ("kvs.html", 200, "", "tube"),
        ("challenge.html", 403, "challenge", "unknown"),
    ],
)
def test_probe_html(fixture: str, status: int, reason: str, kind: str) -> None:
    candidate = SourceCandidate.objects.create(domain="new.test")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["user-agent"].startswith("MarcelineBot")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(status, text=(FIXTURES / fixture).read_text())

    with patch("core.http.time.sleep"):
        probe(candidate, transport=httpx.MockTransport(handler))
    assert paths == ["/robots.txt", "/"]
    assert candidate.kind == kind
    assert reason in candidate.blocked_reason
    if kind == "tube":
        assert candidate.cms_guess == "kvs"
        assert candidate.has_search
        assert candidate.video_links_seen == 2


@pytest.mark.parametrize(
    "status,body",
    [
        (200, "User-agent: *\nDisallow: /"),
        (302, ""),
        (503, ""),
    ],
)
def test_probe_stops_at_robots(status: int, body: str) -> None:
    candidate = SourceCandidate.objects.create(domain="new.test")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(status, text=body, headers={"location": "https://other.test/"})

    probe(candidate, transport=httpx.MockTransport(handler))
    assert paths == ["/robots.txt"]


def test_probe_only_proposed() -> None:
    candidate = SourceCandidate.objects.create(domain="new.test", status="approved")
    with patch("httpx.Client.request", side_effect=AssertionError("network")):
        probe(candidate)
    assert candidate.probed_at is None


def test_admin_approval_and_safe_evidence() -> None:
    candidate = SourceCandidate.objects.create(
        domain="new.test", evidence={"title": '<img src="https://evil.test/"><iframe></iframe>'}
    )
    model_admin = SourceCandidateAdmin(SourceCandidate, AdminSite())
    request = RequestFactory().get("/")
    model_admin.approve(request, SourceCandidate.objects.all())
    model_admin.approve(request, SourceCandidate.objects.all())
    assert Site.objects.count() == 1
    assert not Site.objects.get().enabled
    candidate.refresh_from_db()
    assert candidate.status == "approved"
    model_admin.reset(request, SourceCandidate.objects.all())
    candidate.refresh_from_db()
    assert candidate.status == "proposed"
    assert not Site.objects.exists()
    rendered = model_admin.evidence_text(candidate)
    assert "<img" not in rendered and "<iframe" not in rendered
    audit = SearchCallAdmin(SearchCall, AdminSite())
    assert not audit.has_add_permission(request)
    assert not audit.has_change_permission(request)
    assert not audit.has_delete_permission(request)


def test_admin_mark_torrent() -> None:
    """ADR 0013 amendment 2026-09-17: torrent/P2P sources get their own kind and status,
    alongside `mark_hoster`, never the generic hoster classification."""
    candidate = SourceCandidate.objects.create(domain="p2p-site.example")
    model_admin = SourceCandidateAdmin(SourceCandidate, AdminSite())
    request = RequestFactory().get("/")
    model_admin.mark_torrent(request, SourceCandidate.objects.all())
    candidate.refresh_from_db()
    assert candidate.status == SourceCandidate.Status.TORRENT
    assert candidate.kind == SourceCandidate.Kind.TORRENT


@CREDS
@override_settings(DISCOVERY_MAX_CALLS_PER_RUN=1, DISCOVERY_DAILY_BUDGET_USD="1")
def test_configured_call_cap_cannot_be_raised() -> None:
    client = DataForSEO(
        max_calls=20,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response_data())),
    )
    client.search("first")
    with pytest.raises(SpendLimit):
        client.search("second")
    assert client.calls == 1


@CREDS
def test_old_spend_expires() -> None:
    from datetime import timedelta

    from django.utils import timezone

    old = SearchCall.objects.create(query="old", mode="live", status="ok", cost_usd=1)
    SearchCall.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=25))
    client = DataForSEO(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response_data()))
    )
    client.search("new")
    assert client.calls == 1


@CREDS
def test_discovery_command_upserts_results() -> None:
    out = StringIO()
    with patch(
        "httpx.Client.request",
        return_value=httpx.Response(
            200, json=response_data(), request=httpx.Request("POST", "https://api.dataforseo.com")
        ),
    ):
        call_command("discover_sources", query=["custom title"], stdout=out)
    assert "calls=1 cost=$0.002000 new=1 updated=0" in out.getvalue()
    assert SourceCandidate.objects.get().domain == "newsite.test"
    assert not Site.objects.exists()


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (302, "", "redirect outside registrable domain"),
        (200, '<form><input type="password"></form>', "login"),
        (200, "Please verify your age", "age_self_declaration"),
        (200, "Just a moment...", "challenge"),
    ],
)
def test_homepage_redirect_and_gates(status: int, body: str, expected: str) -> None:
    candidate = SourceCandidate.objects.create(domain="new.test")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(status, text=body, headers={"location": "https://other.test/"})

    with patch("crawler.discovery.probe.PROBE_DELAY_S", 0):
        probe(candidate, transport=httpx.MockTransport(handler))
    assert len(requests) == 2
    assert candidate.http_status == status
    assert candidate.blocked_reason == expected
    assert candidate.kind == "unknown"


@CREDS
def test_task_error_retains_reported_cost() -> None:
    data = response_data("0.0006")
    data["tasks"][0]["status_code"] = 40501
    with pytest.raises(DiscoveryError):
        DataForSEO(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))
        ).search("query")
    assert SearchCall.objects.get().cost_usd == Decimal("0.0006")
    assert SearchCall.objects.get().status == "error"


@CREDS
def test_standard_polling_obeys_call_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/task_post"):
            return httpx.Response(
                200,
                json={
                    "status_code": 20000,
                    "cost": 0.0006,
                    "tasks": [{"status_code": 20100, "id": "pending"}],
                },
            )
        return httpx.Response(200, json={"status_code": 20000, "cost": 0, "tasks": []})

    client = DataForSEO("standard", max_calls=2, transport=httpx.MockTransport(handler))
    with patch("crawler.discovery.dataforseo.time.sleep"), pytest.raises(SpendLimit):
        client.search("query")
    assert client.calls == 2
    assert SearchCall.objects.count() == 2


@CREDS
def test_provider_error_40101_retries_then_succeeds() -> None:
    # Recorded DataForSEO shape for the transient/auth error (40101): retried, not fatal.
    failure = {
        "status_code": 40101,
        "status_message": "Auth error. Please retry.",
        "cost": "0.0001",
    }
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(200, json=failure)
        return httpx.Response(200, json=response_data("0.002"))

    with patch("crawler.discovery.dataforseo.time.sleep") as sleep:
        client = DataForSEO(transport=httpx.MockTransport(handler))
        items = client.search("query")
    assert items[0]["rank"] == 2
    assert len(calls) == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]
    assert client.calls == 3
    assert SearchCall.objects.count() == 3
    assert list(SearchCall.objects.order_by("pk").values_list("status", flat=True)) == [
        "error",
        "error",
        "ok",
    ]


@CREDS
def test_provider_error_40101_retries_exhausted_still_stops_run() -> None:
    failure = {"status_code": 40101, "status_message": "Auth error. Please retry.", "cost": 0}
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=failure)

    with patch("crawler.discovery.dataforseo.time.sleep") as sleep:
        client = DataForSEO(transport=httpx.MockTransport(handler))
        with pytest.raises(ProviderError) as excinfo:
            client.search("query")
    assert excinfo.value.code == 40101
    assert excinfo.value.stops_run  # bounded retries exhausted; still ends the run
    assert len(calls) == 3
    assert sleep.call_count == 2
    assert client.calls == 3
    assert SearchCall.objects.count() == 3


@CREDS
def test_provider_error_40101_retry_cannot_exceed_call_cap() -> None:
    failure = {"status_code": 40101, "status_message": "Auth error. Please retry.", "cost": 0}
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=failure)

    with patch("crawler.discovery.dataforseo.time.sleep") as sleep:
        client = DataForSEO(max_calls=1, transport=httpx.MockTransport(handler))
        with pytest.raises(SpendLimit):
            client.search("query")
    assert len(calls) == 1  # the retry itself is blocked by the call cap, never reaches the wire
    assert sleep.call_count == 1
    assert client.calls == 1
    assert SearchCall.objects.count() == 1


@CREDS
def test_other_40100_range_codes_are_not_retried() -> None:
    data = response_data("0.0001")
    data["status_code"] = 40200
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=data)

    with patch("crawler.discovery.dataforseo.time.sleep") as sleep:
        client = DataForSEO(transport=httpx.MockTransport(handler))
        with pytest.raises(ProviderError) as excinfo:
            client.search("query")
    assert excinfo.value.code == 40200
    assert excinfo.value.stops_run
    assert len(calls) == 1
    sleep.assert_not_called()
    assert client.calls == 1


@CREDS
@pytest.mark.parametrize(
    "code,task_level,calls", [(40104, False, 1), (40200, True, 1), (40501, True, 2)]
)
def test_provider_error_command(code: int, task_level: bool, calls: int) -> None:
    message = (
        "Please verify your account before using the API. You can complete verification "
        "in the user panel: https://app.dataforseo.com/ ."
    )
    failure = {"status_code": code, "status_message": message, "cost": 0}
    data = {"status_code": 20000, "cost": 0, "tasks": [failure]} if task_level else failure
    out, err = StringIO(), StringIO()
    with patch(
        "httpx.Client.request",
        side_effect=[
            httpx.Response(200 if task_level else 403, json=data),
            httpx.Response(200, json=response_data()),
        ],
    ) as request:
        call_command("discover_sources", query=["one", "two"], stdout=out, stderr=err)
    assert request.call_count == calls
    log = SearchCall.objects.order_by("pk").first()
    assert log is not None
    assert log.error == f"Provider error {code}: {message}"
    assert log.error in err.getvalue()
    assert SearchCall.objects.count() == calls


@CREDS
@pytest.mark.parametrize("timeout", [False, True])
def test_http_and_timeout_errors(timeout: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if timeout:
            raise httpx.ConnectTimeout("secret-login secret-pass", request=request)
        return httpx.Response(502, text="secret-login secret-pass")

    with pytest.raises(DiscoveryError):
        DataForSEO(transport=httpx.MockTransport(handler)).search("query")
    assert SearchCall.objects.get().error == ("ConnectTimeout" if timeout else "HTTP 502")


@CREDS
def test_provider_error_redacts_credentials_and_bounds_length() -> None:
    from crawler.discovery.dataforseo import MAX_ERROR_LENGTH

    message = (
        "secret-login secret-pass Authorization: Basic c2VjcmV0LWxvZ2luOnNlY3JldC1wYXNz "
        'login="other-login" password="other-password" Bearer other-token ' + "x" * 2000
    )
    out, err = StringIO(), StringIO()
    with patch(
        "httpx.Client.request",
        return_value=httpx.Response(
            403, json={"status_code": 40104, "status_message": message, "cost": 0}
        ),
    ):
        call_command("discover_sources", query=["one", "two"], stdout=out, stderr=err)
    error = SearchCall.objects.get().error
    assert len(error) == MAX_ERROR_LENGTH
    for secret in (
        "secret-login",
        "secret-pass",
        "c2VjcmV0LWxvZ2luOnNlY3JldC1wYXNz",
        "other-login",
        "other-password",
        "other-token",
        "Authorization",
    ):
        assert secret not in error + out.getvalue() + err.getvalue()


@pytest.mark.parametrize(
    "domain", ["linkedin.com", "creator-wiki.example", "creator-db.example"]
)
def test_reference_sites_ignored(domain: str) -> None:
    assert upsert("https://www." + domain) == (None, False)
    candidate = SourceCandidate.objects.create(domain=domain)
    with patch("core.http.PoliteClient.get", side_effect=AssertionError("network")):
        probe(candidate)
    assert candidate.status == "ignored"


def test_mirror_name() -> None:
    Site.objects.create(domain="www.site03.example")
    candidate, created = upsert("site03.example")
    assert candidate and created
    assert candidate.status == "ignored"
    assert candidate.notes == "mirror of site03.example"
    candidate.status = "rejected"
    candidate.save()
    upsert("site03.example")
    candidate.refresh_from_db()
    assert candidate.status == "rejected"


def test_onboarded_domain_is_not_a_mirror_of_itself() -> None:
    """Operator decision 2026-09-17: a candidate whose domain already exists as a `Site` is
    onboarded (proposed -> approved -> built already, just never closed out here) — never a
    mirror of itself. This is the rule fix behind the site12.example bookkeeping gap."""
    Site.objects.create(domain="onboarded.test")
    candidate = SourceCandidate.objects.create(domain="onboarded.test", kind="tube")

    reason = apply_rules(candidate)

    assert reason == "already onboarded as onboarded.test"
    assert candidate.status == "onboarded"
    assert candidate.notes == "already onboarded as onboarded.test"
    assert "mirror" not in candidate.notes
    assert candidate.evidence.get("mirror_of") is None


def test_same_label_another_tld_is_still_a_mirror() -> None:
    """A genuinely different domain that merely shares a label with an existing Site is
    unaffected by the onboarded fix — it keeps today's mirror behaviour."""
    Site.objects.create(domain="onboarded.test")
    candidate = SourceCandidate.objects.create(domain="onboarded.net")

    reason = apply_rules(candidate)

    assert reason == "mirror of onboarded.test"
    assert candidate.status == "ignored"
    assert candidate.evidence["mirror_of"] == "onboarded.test"


def test_unrelated_domain_is_unchanged_by_apply_rules() -> None:
    Site.objects.create(domain="onboarded.test")
    candidate = SourceCandidate.objects.create(domain="unrelated.test")

    reason = apply_rules(candidate)

    assert reason == ""
    candidate.refresh_from_db()
    assert candidate.status == "proposed"


def test_site12_stale_candidate_becomes_onboarded_not_mirror_of_itself() -> None:
    """The live row this fix exists for: site12.example is approved and built (bespoke adapter,
    fixtures, disabled Site on hold), but its own `SourceCandidate` row was never closed out. `Site.enabled` must
    not matter — only that the Site row exists."""
    Site.objects.create(domain="site12.example", enabled=False)
    candidate = SourceCandidate.objects.create(domain="site12.example", kind="tube")

    reason = apply_rules(candidate)

    assert reason == "already onboarded as site12.example"
    assert candidate.status == "onboarded"
    assert candidate.kind == "tube"  # unchanged: onboarding never guesses a kind
    assert candidate.notes == "already onboarded as site12.example"
    assert candidate.evidence.get("mirror_of") is None


@pytest.mark.parametrize("disallow", [False, True])
def test_redirect_final_robots(disallow: bool) -> None:
    candidate = SourceCandidate.objects.create(domain="new.test")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.host == "new.test":
            return httpx.Response(301, headers={"location": "https://www.new.test/robots.txt"})
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, text="User-agent: *\n" + ("Disallow: /" if disallow else "Allow: /")
            )
        return httpx.Response(200, text="<title>Final host</title>")

    with patch("crawler.discovery.probe.PROBE_DELAY_S", 0):
        probe(candidate, transport=httpx.MockTransport(handler))
    assert urls == ["https://new.test/robots.txt", "https://www.new.test/robots.txt"] + (
        [] if disallow else ["https://www.new.test/"]
    )
    assert candidate.robots_allows is not disallow
    assert candidate.title == ("" if disallow else "Final host")


@pytest.mark.parametrize("stage", ["robots", "home"])
@pytest.mark.parametrize("existing", [False, True])
def test_cross_domain_redirect(stage: str, existing: bool) -> None:
    if existing:
        Site.objects.create(domain="other.test")
    candidate = SourceCandidate.objects.create(domain="new.test")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if stage == "home" and request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://other.test/"})

    with patch("crawler.discovery.probe.PROBE_DELAY_S", 0):
        probe(candidate, transport=httpx.MockTransport(handler))
    assert len(urls) == (1 if stage == "robots" else 2)
    assert all("new.test" in url for url in urls)
    assert candidate.evidence["redirects_to"] == "https://other.test/"
    assert candidate.status == ("ignored" if existing else "proposed")
    if existing:
        assert candidate.notes == "mirror of other.test"


@pytest.mark.parametrize("stage", ["robots", "home"])
def test_redirect_loop_cap(stage: str) -> None:
    from crawler.discovery.probe import MAX_REDIRECT_HOPS

    candidate = SourceCandidate.objects.create(domain="new.test")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if stage == "home" and request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(301, headers={"location": str(request.url)})

    with patch("crawler.discovery.probe.PROBE_DELAY_S", 0):
        probe(candidate, transport=httpx.MockTransport(handler))
    assert len(urls) == MAX_REDIRECT_HOPS + 1 + (stage == "home")
    assert candidate.blocked_reason == "redirect limit reached"


@pytest.mark.parametrize(
    "target,reason",
    [
        ("/welcome", ""),
        ("/private", "redirect disallowed by robots"),
    ],
)
def test_home_redirect_policy(target: str, reason: str) -> None:
    candidate = SourceCandidate.objects.create(domain="new.test")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": target})
        assert request.url.path == "/welcome"
        return httpx.Response(200, text="<title>Welcome</title>")

    with patch("crawler.discovery.probe.PROBE_DELAY_S", 0):
        probe(candidate, transport=httpx.MockTransport(handler))
    assert candidate.blocked_reason == reason
    assert len(urls) == (2 if reason else 3)


@pytest.mark.parametrize(
    "host,robots,final,reason",
    [
        ("www.new.test", "Allow: /", "", ""),
        ("de.new.test", "Allow: /", "", ""),
        ("www.new.test", "Disallow: /", "", "redirect disallowed by robots"),
        (
            "www.new.test",
            "Allow: /",
            "https://m.new.test/",
            "redirect requires unchecked host robots",
        ),
    ],
)
def test_home_redirect_same_domain_host(host: str, robots: str, final: str, reason: str) -> None:
    from crawler.discovery.probe import MAX_PROBE_REQUESTS

    candidate = SourceCandidate.objects.create(domain="new.test")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.host == "new.test":
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /")
            return httpx.Response(301, headers={"location": f"https://{host}/"})
        assert request.url.host == host
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\n" + robots)
        if final:
            return httpx.Response(302, headers={"location": final})
        return httpx.Response(200, text="<title>Final host</title>")

    with patch("crawler.discovery.probe.PROBE_DELAY_S", 0):
        probe(candidate, transport=httpx.MockTransport(handler))
    expected = ["https://new.test/robots.txt", "https://new.test/", f"https://{host}/robots.txt"]
    if "disallowed" not in reason:
        expected.append(f"https://{host}/")
    assert urls == expected
    assert len([url for url in urls if url.endswith("/robots.txt")]) + 1 <= MAX_PROBE_REQUESTS
    assert candidate.blocked_reason == reason
    assert candidate.title == ("" if reason else "Final host")
    assert candidate.robots_allows is not ("disallowed" in reason)
    assert candidate.evidence["redirects_to"] == (final or f"https://{host}/")


def test_reapply_rules_command() -> None:
    Site.objects.create(domain="www.site03.example")
    for domain in ("creator-wiki.example", "site03.example", "site03.example", "luluvdo.com", "t.me", "new.test"):
        SourceCandidate.objects.create(domain=domain)
    SourceCandidate.objects.create(domain="creator-db.example", status="rejected")
    SourceCandidate.objects.create(domain="site03.example", status="approved")
    before = dict(SourceCandidate.objects.values_list("domain", "status"))
    out = StringIO()
    with patch("httpx.Client.request", side_effect=AssertionError("network")):
        call_command("discover_sources", reapply_rules=True, dry_run=True, stdout=out)
        assert dict(SourceCandidate.objects.values_list("domain", "status")) == before
        assert "site03.example: proposed -> ignored (kind unknown; mirror of site03.example)" in (
            out.getvalue()
        )
        # site03.example *is* the Site's own domain (normalized off "www."): onboarded, never a
        # mirror of itself (operator decision 2026-09-17, the site12.example bug).
        assert "site03.example: proposed -> onboarded (kind unknown; already onboarded as " in (
            out.getvalue()
        )
        assert "would change=5" in out.getvalue()
        call_command("discover_sources", reapply_rules=True, stdout=StringIO())
    after = {row.domain: row for row in SourceCandidate.objects.all()}
    assert after["creator-wiki.example"].status == "ignored"
    assert after["creator-wiki.example"].notes == "ignored non-source domain"
    assert after["site03.example"].status == "ignored"
    assert after["site03.example"].evidence["mirror_of"] == "site03.example"
    assert after["site03.example"].status == "onboarded"
    assert after["site03.example"].notes == "already onboarded as site03.example"
    assert after["site03.example"].evidence.get("mirror_of") is None
    assert (after["luluvdo.com"].kind, after["luluvdo.com"].status) == ("file_hoster", "hoster")
    assert (after["t.me"].kind, after["t.me"].status) == ("social", "ignored")
    assert after["new.test"].status == "proposed"
    assert after["creator-db.example"].status == "rejected"
    assert after["site03.example"].status == "approved"
    out = StringIO()
    call_command("discover_sources", reapply_rules=True, stdout=out)
    assert "changed=0" in out.getvalue()
    with pytest.raises(CommandError):
        call_command("discover_sources", reapply_rules=True, from_links=True)


def test_retry_failed_command() -> None:
    from django.utils import timezone

    retry: list[int] = []
    for status, reason in [(301, "robots unavailable"), (302, ""), (301, "redirect limit reached")]:
        retry.append(
            SourceCandidate.objects.create(
                domain=f"retry{len(retry)}.test",
                probed_at=timezone.now(),
                http_status=status,
                blocked_reason=reason,
            ).pk
        )
    for status, reason in [(503, "robots unavailable"), (200, ""), (403, "challenge")]:
        SourceCandidate.objects.create(
            domain=f"skip{status}.test",
            probed_at=timezone.now(),
            http_status=status,
            blocked_reason=reason,
        )
    SourceCandidate.objects.create(domain="fresh.test")
    SourceCandidate.objects.create(
        domain="ignored.test", status="ignored", http_status=301, probed_at=timezone.now()
    )
    with patch("crawler.management.commands.probe_candidates.probe") as mocked:
        call_command("probe_candidates", retry_failed=True, stdout=StringIO())
    assert [call.args[0].pk for call in mocked.call_args_list] == retry


def test_domain_probes_a_candidate_of_any_status() -> None:
    """Naming a domain is an operator decision: an approved domain still needs its probe."""
    approved = SourceCandidate.objects.create(domain="approved.test", status="approved")
    SourceCandidate.objects.create(domain="other.test")

    with patch("crawler.management.commands.probe_candidates.probe") as mocked:
        call_command("probe_candidates", domain="approved.test", stdout=StringIO())

    assert [call.args[0].pk for call in mocked.call_args_list] == [approved.pk]
    assert mocked.call_args_list[0].kwargs == {"force": True}


def test_probe_follows_a_www_redirect_without_calling_it_a_mirror() -> None:
    """A hop inside the candidate's own registrable domain is not evidence of a mirror.

    An approved candidate has a Site with its own domain, so the mirror rule matched itself and
    ignored the candidate on a plain apex->www redirect (2026-09-16).
    """
    Site.objects.create(domain="approved.test")
    candidate = SourceCandidate.objects.create(domain="approved.test", status="approved")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "approved.test":
            return httpx.Response(
                301, headers={"location": f"https://www.approved.test{request.url.path}"}
            )
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="<html><title>Approved</title></html>")

    with patch("core.http.time.sleep"):
        probe(candidate, transport=httpx.MockTransport(handler), force=True)

    candidate.refresh_from_db()
    assert candidate.status == "approved"
    assert candidate.evidence.get("mirror_of") is None
    assert candidate.blocked_reason != "redirect to existing Site"


def test_probe_never_reclassifies_a_reviewed_candidate() -> None:
    """Approving a candidate creates a Site with its domain, which the mirror rule then matches.

    Re-applying the offline rules to a reviewed row therefore ignores it as a mirror of itself and
    silently destroys the operator's decision (2026-09-16).
    """
    Site.objects.create(domain="approved.test")
    candidate = SourceCandidate.objects.create(domain="approved.test", status="approved")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="<html><title>Approved</title></html>")

    with patch("core.http.time.sleep"):
        probe(candidate, transport=httpx.MockTransport(handler), force=True)

    candidate.refresh_from_db()
    assert candidate.status == "approved"
    assert "mirror" not in candidate.notes
    assert candidate.probed_at is not None


def test_probe_forced_reaches_an_approved_candidate() -> None:
    """The status guard lives in `probe` itself, so selecting the row is not enough."""
    candidate = SourceCandidate.objects.create(domain="approved.test", status="approved")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="<html><title>Approved</title></html>")

    with patch("core.http.time.sleep"):
        probe(candidate, transport=httpx.MockTransport(handler), force=True)

    assert paths == ["/robots.txt", "/"]
    assert candidate.probed_at is not None
    assert candidate.http_status == 200


def test_robots_http_upgrade_and_delay() -> None:
    from core.http import PoliteClient
    from crawler.discovery.probe import fetch

    candidate = SourceCandidate.objects.create(domain="new.test")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"location": "https://new.test/robots.txt"})
        return httpx.Response(200, text="User-agent: *\nDisallow: /")

    with (
        PoliteClient(
            "MarcelineBot/0.1", transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client,
        patch.object(client, "_wait_turn") as wait,
    ):
        response = fetch(client, candidate, "http://new.test/robots.txt")
    assert response and response.status == 200
    assert urls == ["http://new.test/robots.txt", "https://new.test/robots.txt"]
    assert [call.args[1] for call in wait.call_args_list] == [5.0, 5.0]


INVITE_CODE = "AbCdEfGh1234_x-Y"


@pytest.mark.parametrize(
    "url,identity,post_id",
    [
        ("https://t.me/Somechannel", "SomeChannel", None),
        ("http://telegram.me/Some_Channel?start=1#frag", "some_channel", None),
        ("https://www.t.me/s/SomeChannel", "somechannel", None),
        ("https://t.me/s/SomeChannel/123?before=100", "somechannel", 123),
        ("https://T.ME/some_channel/456?comment=2#c", "some_channel", 456),
        ("t.me/Channel_1/", "channel_1", None),
        ("https://t.me/channel1/not-a-post", "channel1", None),
    ],
)
def test_parse_telegram_channel(url: str, identity: str, post_id: int | None) -> None:
    from crawler.discovery.telegram import TelegramLink, parse_telegram_link

    assert parse_telegram_link(url) == TelegramLink(False, identity, post_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://t.me/SomeBot",
        "https://t.me/some_bot?start=promo",
        "https://t.me/share/url?url=https://x.test",
        "https://t.me/iv?url=https://x.test&rhash=1",
        "https://t.me/addstickers/Pack",
        "https://t.me/addlist/abcdefghij",
        "https://t.me/proxy?server=192.0.2.4",
        "https://t.me/c/123456/7",
        "https://t.me/telegram",
        "https://t.me/s/",
        "https://t.me/",
        "https://t.me/abc",
        "https://t.me/1channel",
        "https://t.me/bad__name",
        "https://t.me/joinchat/",
        "https://t.me/+short",
        "https://example.com/channel",
        "https://t.me.evil.test/channel",
        "https://evil.test/t.me/channel",
        "ftp://t.me/channel",
        "https://[invalid/",
    ],
)
def test_parse_telegram_ignored(url: str) -> None:
    from crawler.discovery.telegram import parse_telegram_link

    assert parse_telegram_link(url) is None


def test_parse_invite_links_hashed() -> None:
    from crawler.discovery.telegram import hash_invite, parse_telegram_link

    expected = hash_invite(INVITE_CODE)
    for url in (
        f"https://t.me/+{INVITE_CODE}",
        f"https://telegram.me/joinchat/{INVITE_CODE}?x=1#y",
        f"https://t.me/%2B{INVITE_CODE}",
        f"https://t.me/JoinChat/{INVITE_CODE}/",
    ):
        parsed = parse_telegram_link(url)
        assert parsed is not None and parsed.private_invite
        assert parsed.identity == expected and parsed.post_id is None
        assert INVITE_CODE.lower() not in parsed.identity.lower()
    other = parse_telegram_link(f"https://t.me/+{INVITE_CODE.lower()}")
    assert other is not None and other.identity != expected  # invite codes are case-sensitive


def test_mine_channels_evidence_and_dedupe(make_page: Callable[[str], Page]) -> None:
    from crawler.discovery.services import ChannelCounts, mine_channels
    from crawler.models import TelegramChannelCandidate

    other_site = Site.objects.create(domain="other.test")
    alice, bob = Creator.objects.create(name="Alice"), Creator.objects.create(name="Bob")
    pages = [make_page(f"p{i}") for i in range(3)]
    pages[2].site = other_site
    pages[2].save()
    pages[0].creators.add(alice)
    pages[1].creators.add(alice, bob)
    links = [
        ["https://t.me/PromoChan", "https://t.me/s/promochan/12", f"https://t.me/+{INVITE_CODE}"],
        ["https://telegram.me/promochan/15?single", "https://t.me/helperbot"],
        ["https://t.me/promochan", f"https://t.me/joinchat/{INVITE_CODE}", "not a url"],
    ]
    for page, page_links in zip(pages, links, strict=True):
        page.raw_extraction = {"external_links": page_links}
        page.save()
    blocked = make_page("blocked")
    blocked.safety_status = Page.SafetyStatus.BLOCKED
    blocked.raw_extraction = {"external_links": ["https://t.me/blockedchan"]}
    blocked.save()
    make_page("bad").raw_extraction = {"external_links": "https://t.me/x"}

    assert mine_channels().channels_new == 1
    channel = TelegramChannelCandidate.objects.get(private_invite=False)
    assert channel.identity == "promochan" and channel.status == "proposed"
    assert (channel.linking_pages, channel.distinct_creators, channel.distinct_sites) == (3, 2, 2)
    assert channel.sites == ["example.test", "other.test"]
    assert set(channel.sample_pages) == {page.url for page in pages}
    assert channel.sample_post_ids == [15, 12]
    invite = TelegramChannelCandidate.objects.get(private_invite=True)
    assert invite.status == "ignored" and "off-limits" in invite.notes
    assert invite.linking_pages == 2
    assert INVITE_CODE not in json.dumps(
        list(TelegramChannelCandidate.objects.values()), default=str
    )
    assert not TelegramChannelCandidate.objects.filter(identity="blockedchan").exists()

    channel.status, channel.notes = "rejected", "operator note"
    channel.save()
    for i in range(EVIDENCE_LIMIT + 5):
        page = make_page(f"extra{i}")
        page.raw_extraction = {"external_links": [f"https://t.me/promochan/{100 + i}"]}
        page.save()
    counts = mine_channels()
    assert counts == ChannelCounts(channels=1, channels_new=0, invites=1, invites_new=0)
    assert mine_channels() == counts
    channel.refresh_from_db()
    assert TelegramChannelCandidate.objects.count() == 2
    assert (channel.status, channel.notes) == ("rejected", "operator note")
    assert channel.linking_pages == 3 + EVIDENCE_LIMIT + 5
    assert len(channel.sample_pages) == EVIDENCE_LIMIT
    assert channel.sample_post_ids[:2] == [124, 123]
    assert len(channel.sample_post_ids) == EVIDENCE_LIMIT


def test_from_links_telegram_command(make_page: Callable[[str], Page]) -> None:
    from crawler.models import TelegramChannelCandidate

    page = make_page("tg")
    page.raw_extraction = {
        "external_links": [
            "https://t.me/ChanOne",
            "https://t.me/s/chantwo/9",
            f"https://t.me/+{INVITE_CODE}",
            "https://www.new.test/a",
        ]
    }
    page.save()
    out = StringIO()
    with patch("httpx.Client.request", side_effect=AssertionError("network")):
        call_command("discover_sources", from_links=True, dry_run=True, stdout=out)
        assert "Telegram plan: channels=2 invite_links=1" in out.getvalue()
        assert not TelegramChannelCandidate.objects.exists()
        assert not SourceCandidate.objects.exists()
        out = StringIO()
        call_command("discover_sources", from_links=True, stdout=out)
        assert "channels=2 (new 2) invite_links=1 (new 1)" in out.getvalue()
        out = StringIO()
        call_command("discover_sources", from_links=True, stdout=out)
        assert "channels=2 (new 0) invite_links=1 (new 0)" in out.getvalue()
    assert TelegramChannelCandidate.objects.count() == 3
    assert SourceCandidate.objects.get(domain="t.me").status == "ignored"
    assert not SearchCall.objects.exists()


def test_telegram_admin() -> None:
    from django.db import IntegrityError, transaction

    from crawler.admin import TelegramChannelCandidateAdmin
    from crawler.discovery.telegram import hash_invite
    from crawler.models import TelegramChannelCandidate

    channel = TelegramChannelCandidate.objects.create(identity="promochan", linking_pages=2)
    invite = TelegramChannelCandidate.objects.create(
        private_invite=True, identity=hash_invite(INVITE_CODE), status="ignored", linking_pages=5
    )
    model_admin = TelegramChannelCandidateAdmin(TelegramChannelCandidate, AdminSite())
    request = RequestFactory().get("/")
    assert list(model_admin.get_queryset(request).order_by(*model_admin.ordering or ())) == [
        invite,
        channel,
    ]
    assert model_admin.list_filter == ("status", "private_invite")
    assert "notes" not in model_admin.readonly_fields
    link = model_admin.channel(channel)
    assert link.startswith('<a href="https://t.me/promochan"')
    assert "<img" not in link and "<iframe" not in link
    assert "<a" not in model_admin.channel(invite) and "off-limits" in model_admin.channel(invite)
    assert not model_admin.has_add_permission(request)
    model_admin.approve(request, TelegramChannelCandidate.objects.all())
    channel.refresh_from_db()
    invite.refresh_from_db()
    assert (channel.status, invite.status) == ("approved", "ignored")
    assert not Site.objects.exists() and not SourceCandidate.objects.exists()
    with transaction.atomic(), pytest.raises(IntegrityError):
        TelegramChannelCandidate.objects.filter(pk=invite.pk).update(status="approved")
    with transaction.atomic(), pytest.raises(IntegrityError):
        TelegramChannelCandidate.objects.create(identity="promochan")


def test_invalid_redirect() -> None:
    candidate = SourceCandidate.objects.create(domain="new.test")
    probe(
        candidate,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(301, headers={"location": "https://[invalid/"})
        ),
    )
    assert candidate.blocked_reason == "redirect invalid"
