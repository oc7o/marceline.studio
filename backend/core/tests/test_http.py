from collections.abc import Callable, Iterable, Iterator

import httpcore
import httpx
import pytest

from core.http import (
    AddressNotAllowedError,
    FetchResponse,
    PoliteClient,
    PublicOnlyBackend,
    PublicOnlyTransport,
    RequestRefusedError,
    ResponseTooLargeError,
    is_public_address,
)

UA = "MarcelineBot/0.1"


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    fake: FakeTime,
    *,
    max_retries: int = 3,
    max_bytes: int = 1024,
) -> PoliteClient:
    return PoliteClient(
        UA,
        max_retries=max_retries,
        max_bytes=max_bytes,
        transport=httpx.MockTransport(handler),
        clock=fake.clock,
        sleep=fake.sleep,
    )


def test_sends_user_agent_and_follows_redirects() -> None:
    seen_agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_agents.append(request.headers["user-agent"])
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text="hello", headers={"content-type": "text/html"})

    response = make_client(handler, FakeTime()).get("https://site.test/old")

    assert response.url == "https://site.test/new"
    assert response.status == 200
    assert response.text == "hello"
    assert seen_agents == [UA, UA]


def test_retries_server_errors_with_exponential_backoff() -> None:
    statuses = [503, 502, 200]
    fake = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(statuses.pop(0))

    response = make_client(handler, fake).get("https://site.test/")

    assert response.status == 200
    assert response.attempts == 3
    assert fake.sleeps == [2.0, 4.0]


def test_honors_retry_after_header() -> None:
    responses = [httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200)]
    fake = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    assert make_client(handler, fake).get("https://site.test/").status == 200
    assert fake.sleeps == [7.0]


def test_returns_last_error_response_when_retries_are_exhausted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    response = make_client(handler, FakeTime(), max_retries=1).get("https://site.test/")

    assert response.status == 500
    assert response.attempts == 2


def test_raises_network_error_after_retries() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(httpx.ConnectError):
        make_client(handler, FakeTime(), max_retries=1).get("https://site.test/")
    assert len(calls) == 2


def test_rejects_oversized_bodies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 20)

    with pytest.raises(ResponseTooLargeError):
        make_client(handler, FakeTime(), max_bytes=10).get("https://site.test/")


def test_per_request_max_bytes_overrides_the_client_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 20)

    assert make_client(handler, FakeTime(), max_bytes=10).get(
        "https://site.test/", max_bytes=100
    ).body == (b"x" * 20)
    with pytest.raises(ResponseTooLargeError):
        make_client(handler, FakeTime(), max_bytes=1024).get("https://site.test/", max_bytes=10)


def test_stops_reading_as_soon_as_the_limit_is_exceeded() -> None:
    yielded: list[int] = []

    def chunks() -> Iterator[bytes]:
        for index in range(100):
            yielded.append(index)
            yield b"x" * 10

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    with pytest.raises(ResponseTooLargeError):
        make_client(handler, FakeTime()).get("https://site.test/", max_bytes=25)
    assert len(yielded) == 3


def test_waits_between_requests_to_the_same_host_only() -> None:
    fake = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    client = make_client(handler, fake)
    client.get("https://a.test/1", min_delay_s=5)
    client.get("https://b.test/1", min_delay_s=5)
    client.get("https://a.test/2", min_delay_s=5)

    assert fake.sleeps == [5.0]


def test_decodes_body_with_declared_charset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content="café".encode("latin-1"),
            headers={"content-type": "text/html; charset=ISO-8859-1"},
        )

    assert make_client(handler, FakeTime()).get("https://site.test/").text == "café"


def test_decodes_html_with_a_meta_charset_when_the_header_has_none() -> None:
    page = '<html><head><meta charset="windows-1252"><title>café</title>'.encode("cp1252")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=page, headers={"content-type": "text/html"})

    assert "café" in make_client(handler, FakeTime()).get("https://site.test/").text


def test_meta_http_equiv_charset_and_header_precedence() -> None:
    body = '<meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">é'.encode(
        "latin-1"
    )
    assert FetchResponse("u", 200, {"content-type": "text/html"}, body).encoding == "iso8859-1"
    headed = {"content-type": "text/html; charset=utf-8"}
    assert FetchResponse("u", 200, headed, body).encoding == "utf-8"
    assert FetchResponse("u", 200, {"content-type": "image/jpeg"}, body).encoding == "utf-8"
    bogus = b'<meta charset="no-such-codec">'
    assert FetchResponse("u", 200, {}, bogus).encoding == "utf-8"


# --- hooks, hop by hop -------------------------------------------------------------------------


def test_before_request_runs_after_the_wait_and_before_every_attempt_and_hop() -> None:
    fake = FakeTime()
    events: list[str] = []
    responses = [
        httpx.Response(200),
        httpx.Response(503),
        httpx.Response(302, headers={"location": "https://cdn.test/final"}),
        httpx.Response(200),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(f"request {request.url}")
        return responses.pop(0)

    def sleep(seconds: float) -> None:
        events.append("sleep")
        fake.sleep(seconds)

    client = PoliteClient(
        UA, max_retries=1, transport=httpx.MockTransport(handler), clock=fake.clock, sleep=sleep
    )
    client.get("https://site.test/x", min_delay_s=5)
    events.clear()

    client.get("https://site.test/x", min_delay_s=5, before_request=lambda u: events.append(u))

    assert events == [
        "sleep",  # the host's turn
        "https://site.test/x",
        "request https://site.test/x",
        "sleep",  # backoff after the 503
        "sleep",  # the rest of the host's turn
        "https://site.test/x",
        "request https://site.test/x",
        "https://cdn.test/final",
        "request https://cdn.test/final",
    ]


def test_a_refusing_hook_sends_nothing_and_takes_no_slot() -> None:
    fake = FakeTime()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200)

    def refuse(url: str) -> None:
        raise RequestRefusedError("page blocked meanwhile")

    client = make_client(handler, fake)
    with pytest.raises(RequestRefusedError):
        client.get("https://site.test/", min_delay_s=5, before_request=refuse)
    client.get("https://site.test/", min_delay_s=5)

    assert requests == ["https://site.test/"]
    assert fake.sleeps == []


def test_on_redirect_sees_the_target_before_it_is_requested() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.test/landing"})

    def allowlist(url: str) -> None:
        if not url.startswith("https://site.test/"):
            raise RequestRefusedError(f"redirected off-site to {url}")

    with pytest.raises(RequestRefusedError, match=r"elsewhere\.test"):
        make_client(handler, FakeTime()).get("https://site.test/a", on_redirect=allowlist)
    assert requests == ["https://site.test/a"]


def test_redirects_are_capped_and_can_be_returned_as_they_are() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "/again"})

    with pytest.raises(httpx.TooManyRedirects):
        make_client(handler, FakeTime()).get("https://site.test/")
    unfollowed = PoliteClient(
        UA, transport=httpx.MockTransport(handler), follow_redirects=False
    ).get("https://site.test/")
    assert (unfollowed.status, unfollowed.headers["location"]) == (301, "/again")


def test_every_redirect_hop_waits_for_its_host() -> None:
    fake = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200)

    make_client(handler, fake).get("https://site.test/old", min_delay_s=5)

    assert fake.sleeps == [5.0]


# --- public_only -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("2606:4700::1", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("240.0.0.1", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("fc00::1", False),
        ("::ffff:127.0.0.1", False),
        ("64:ff9b::a9fe:a9fe", False),
        ("64:ff9b::5db8:d822", True),
        ("not-an-address", False),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    assert is_public_address(address) is public


def resolver_for(table: dict[str, list[str]]) -> Callable[[str], list[str]]:
    return lambda host: table.get(host, [])


def public_client(
    handler: Callable[[httpx.Request], httpx.Response], table: dict[str, list[str]]
) -> PoliteClient:
    return PoliteClient(
        UA,
        max_retries=0,
        transport=httpx.MockTransport(handler),
        public_only=True,
        resolver=resolver_for(table),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/admin/",
        "http://[::1]/",
        "http://internal.test/",
        "http://mixed.test/",
        "http://unresolved.test/",
        "ftp://cdn.test/a.jpg",
    ],
)
def test_public_only_refuses_before_sending(url: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    table = {
        "cdn.test": ["93.184.216.34"],
        "internal.test": ["10.0.0.5"],
        "mixed.test": ["93.184.216.34", "192.168.0.2"],
    }
    with pytest.raises(AddressNotAllowedError):
        public_client(handler, table).get(url)
    assert requests == []


@pytest.mark.parametrize(
    "location",
    ["http://169.254.169.254/latest/meta-data/", "http://internal.test/", "file:///etc/passwd"],
)
def test_public_only_checks_every_redirect_hop(location: str) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    table = {"cdn.test": ["93.184.216.34"], "internal.test": ["10.0.0.5"]}
    with pytest.raises(AddressNotAllowedError):
        public_client(handler, table).get("https://cdn.test/a.jpg")
    assert requests == ["https://cdn.test/a.jpg"]


class RecordingBackend(httpcore.NetworkBackend):
    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        self.connected.append((host, port))
        raise httpcore.ConnectError("recorded, not connected")


def test_the_backend_connects_to_the_address_it_checked() -> None:
    """DNS rebinding: the name is resolved once and the connection goes to the checked address."""
    answers = [["93.184.216.34"], ["127.0.0.1"]]
    inner = RecordingBackend()
    backend = PublicOnlyBackend(lambda host: answers.pop(0), inner=inner)

    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("cdn.test", 443)
    with pytest.raises(AddressNotAllowedError):
        backend.connect_tcp("cdn.test", 443)
    with pytest.raises(AddressNotAllowedError):
        backend.connect_unix_socket("/var/run/docker.sock")

    assert inner.connected == [("93.184.216.34", 443)]


def test_a_public_only_client_uses_the_checked_backend() -> None:
    with PoliteClient(UA, public_only=True, resolver=resolver_for({})) as client:
        transport = client._client._transport
        assert isinstance(transport, PublicOnlyTransport)
        assert isinstance(transport.network_backend, PublicOnlyBackend)
        with pytest.raises(AddressNotAllowedError):
            client.get("https://cdn.test/a.jpg")
