"""PoliteClient reports every attempt to its observer (ADR 0007 amendment 2026-09-17)."""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from core.http import (
    BudgetExhaustedError,
    PoliteClient,
    RequestAttempt,
    RequestLabel,
    RequestRefusedError,
    ResponseTooLargeError,
)

UA = "MarcelineBot/0.1"


class Recorder:
    def __init__(self, refuse: Callable[[RequestAttempt], bool] = lambda attempt: False) -> None:
        self.refuse = refuse
        self.admitted: list[tuple[str, str]] = []
        self.records: list[tuple[str, str, str, int]] = []
        self.flushes = 0

    def admit(self, attempt: RequestAttempt) -> None:
        if self.refuse(attempt):
            raise BudgetExhaustedError(
                "budget", scope="origin", host=attempt.host, resume_at=datetime.now(UTC)
            )
        self.admitted.append((attempt.url, attempt.request_type))

    def record(self, attempt: RequestAttempt, outcome: str, size: int) -> None:
        self.records.append((attempt.host, attempt.request_type, outcome, size))

    def flush(self) -> None:
        self.flushes += 1


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    recorder: Recorder,
    **kwargs: object,
) -> PoliteClient:
    return PoliteClient(
        UA,
        transport=httpx.MockTransport(handler),
        sleep=lambda seconds: None,
        observer=recorder,
        **kwargs,  # type: ignore[arg-type]
    )


def test_every_attempt_hop_and_retry_is_recorded_with_its_type() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://cdn.test/new"})
        if calls.count("/new") == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=b"12345")

    recorder = Recorder()
    client = make_client(handler, recorder)
    client.get("https://site.test/old", label=RequestLabel("page", site_id=7))

    assert recorder.records == [
        ("site.test", "page", "3xx", 0),
        ("cdn.test", "redirect_hop", "5xx", 0),
        ("cdn.test", "retry", "2xx", 5),
    ]
    assert [kind for _, kind in recorder.admitted] == ["page", "redirect_hop", "retry"]


def test_network_errors_and_oversized_bodies_are_recorded() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    recorder = Recorder()
    with pytest.raises(httpx.ConnectError):
        make_client(failing, recorder, max_retries=1).get("https://site.test/")
    assert [(kind, outcome) for _, kind, outcome, _ in recorder.records] == [
        ("other", "error"),
        ("retry", "error"),
    ]

    recorder = Recorder()
    big = make_client(lambda request: httpx.Response(200, content=b"x" * 50), recorder)
    with pytest.raises(ResponseTooLargeError):
        big.get("https://site.test/big", max_bytes=10)
    assert recorder.records == [("site.test", "other", "2xx", 0)]


def test_budget_refusal_sends_nothing_and_is_recorded_as_refused() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200)

    recorder = Recorder(refuse=lambda attempt: True)
    with pytest.raises(BudgetExhaustedError):
        make_client(handler, recorder).get("https://site.test/")
    assert sent == []
    assert recorder.records == [("site.test", "other", "refused", 0)]


def test_hook_refusals_are_recorded_and_never_reach_the_budget_check() -> None:
    def deny(url: str) -> None:
        raise RequestRefusedError("denied")

    recorder = Recorder()
    client = make_client(lambda request: httpx.Response(200), recorder)
    with pytest.raises(RequestRefusedError):
        client.get("https://site.test/", before_request=deny)
    assert recorder.admitted == []
    assert recorder.records == [("site.test", "other", "refused", 0)]

    recorder = Recorder()
    redirecting = make_client(
        lambda request: httpx.Response(302, headers={"location": "https://off.test/"}), recorder
    )
    with pytest.raises(RequestRefusedError):
        redirecting.get("https://site.test/", on_redirect=deny)
    assert recorder.records == [
        ("site.test", "other", "3xx", 0),
        ("off.test", "redirect_hop", "refused", 0),
    ]


def test_default_label_applies_and_close_flushes() -> None:
    recorder = Recorder()
    client = make_client(
        lambda request: httpx.Response(404), recorder, label=RequestLabel("rdap", role="x")
    )
    client.get("https://rdap.test/ip/1")
    assert recorder.records == [("rdap.test", "rdap", "4xx", 0)]
    client.close()
    assert recorder.flushes == 1


def test_accounting_failures_do_not_fail_the_request() -> None:
    class Broken(Recorder):
        def record(self, attempt: RequestAttempt, outcome: str, size: int) -> None:
            raise RuntimeError("database down")

    response = make_client(lambda request: httpx.Response(200), Broken()).get("https://a.test/")
    assert response.status == 200
