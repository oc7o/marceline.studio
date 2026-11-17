"""Polite HTTP client for crawling.

Honest User-Agent, per-host minimum delay between requests, a response size cap, and retries with
backoff on 429/5xx and network errors. Knows nothing about sites or robots.txt: callers pass the
delay they need.

**Redirects are followed here, one hop at a time**, never inside httpx, so every hop is a request
like any other: it waits for its host's turn, is retried on its own and passes the same checks.
Callers hook into that sequence instead of inspecting the final URL afterwards, when the request to
wherever a redirect pointed has already been made:

- `before_request(url)` runs after the per-host wait and immediately before *every* attempt —
  each retry and each redirect hop. The media deny re-reads the page's safety state there, so a
  page blocked while a worker waited for its slot is never requested (issue #65);
- `on_redirect(url)` runs for every redirect target before it is requested. The crawler checks the
  site allowlist and robots.txt there.

A hook refuses by raising; its exception propagates unchanged and nothing more is sent. A refused
attempt does not take the host's slot.

**Host gate** (ADR 0030): the wait for a host's turn is a `HostGate`. `InMemoryHostGate` (the
default) keeps the last request start per host in this client, as before. The crawl runtime passes
`core.host_slots.DbHostGate`, which keeps one slot per host in Postgres, shared by every thread and
process. Per attempt: `acquire` (wait, take the host's lease), the hooks and checks, then `start`
(the delay counts from here) or `cancel` (refused: the slot is untouched), and `done` once the
response was read or failed. Backoffs after 429/5xx or network errors go through `penalize`: a
sleep in memory, a host-wide penalty in the database.

**Request accounting** (ADR 0007 amendment 2026-09-17): a client built with an `observer` reports
every attempt to it — each retry, each redirect hop, and refusals by a hook — so this client is
the one place outgoing requests are counted. `RequestObserver.admit` runs last, after the hooks
and the `public_only` check and immediately before the request; it enforces request budgets by
raising `BudgetExhaustedError`. The caller says what a request is for with a `RequestLabel`; the
attempt number and hop decide whether it is counted as a `retry` or `redirect_hop` instead.

**`public_only`** (the review image passthrough and the media worker): every hop must be `http(s)`
to a host whose addresses are all public — no loopback, private, link-local, shared, reserved or
multicast address.
The real transport (`PublicOnlyTransport`) resolves each hostname once, checks those addresses and
connects to a checked address itself (TLS still verifies the hostname), so a DNS answer that
changes between the check and the connection (rebinding) cannot reach an internal address.
"""

import codecs
import contextlib
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Protocol, Self
from urllib.parse import urlsplit

import httpcore
import httpx

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
#: How far into an HTML body `<meta charset>` is looked for (the HTML encoding prescan's window).
META_PRESCAN_BYTES = 1024
META_CHARSET = re.compile(rb"""<meta[^>]*?charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.I)
#: NAT64 addresses embed the IPv4 address the gateway really connects to.
NAT64 = ipaddress.IPv6Network("64:ff9b::/96")

UrlHook = Callable[[str], None]
Resolver = Callable[[str], list[str]]


@dataclass(frozen=True)
class RequestLabel:
    """What a request is for, as its caller knows it.

    `kind` is the request type (`page`, `robots`, `media`, `probe`, `rdap`, ...), `site_id` the
    catalog site it is made for (None for traffic that belongs to no site), `role` the host role
    when the caller knows it (blank: the observer classifies the host), and `enforce` whether
    budgets apply (False: counted only).
    """

    kind: str = "other"
    site_id: int | None = None
    role: str = ""
    enforce: bool = True


@dataclass(frozen=True)
class RequestAttempt:
    """One attempt at one URL: `hop` 0 is the URL asked for, `attempt` 1 the first try."""

    url: str
    host: str
    label: RequestLabel = field(default_factory=RequestLabel)
    hop: int = 0
    attempt: int = 1

    @property
    def request_type(self) -> str:
        if self.attempt > 1:
            return REQUEST_RETRY
        if self.hop > 0:
            return REQUEST_REDIRECT_HOP
        return self.label.kind


def outcome_class(status: int | None) -> str:
    """`2xx`..`5xx` for a status, `error` without a usable one."""
    if status is None or not 100 <= status <= 599:
        return OUTCOME_ERROR
    return f"{status // 100}xx"


class RequestObserver(Protocol):
    """Counts a `PoliteClient`'s requests and enforces budgets on them."""

    def admit(self, attempt: RequestAttempt) -> None:
        """Raise `BudgetExhaustedError` (or another `RequestRefusedError`) to refuse."""

    def record(self, attempt: RequestAttempt, outcome: str, size: int) -> None:
        """`outcome` from `outcome_class`, or `refused`; `size` is the body bytes received."""

    def flush(self) -> None:
        """Persist what was recorded so far."""


# Request types the client derives itself; callers name the others (`RequestLabel.kind`).
REQUEST_RETRY = "retry"
REQUEST_REDIRECT_HOP = "redirect_hop"
# Outcome classes besides `2xx`..`5xx`: no response at all, and refused before sending.
OUTCOME_ERROR = "error"
OUTCOME_REFUSED = "refused"

logger = logging.getLogger(__name__)


class ResponseTooLargeError(Exception):
    """The response body is larger than the client's size limit."""

    def __init__(self, message: str, *, status: int | None = None, bytes_read: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.bytes_read = bytes_read


class RequestRefusedError(Exception):
    """A request was refused before it was sent. `reason` is short and safe to show or log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BudgetExhaustedError(RequestRefusedError):
    """A request budget is used up; nothing was sent. Try again at `resume_at` at the earliest.

    `scope` names the budget (`origin:page` / `origin:media`: one kind of request to a site's own
    hosts, `cdn`: one CDN host across sites) and `host` the host the refused request was for.
    """

    def __init__(self, reason: str, *, scope: str, host: str, resume_at: datetime) -> None:
        super().__init__(reason)
        self.scope = scope
        self.host = host
        self.resume_at = resume_at


class HostBusyError(RequestRefusedError):
    """The host's slot did not come free within the gate's maximum wait; nothing was sent.

    Not a verdict on the URL: callers put the work back with the attempt refunded and try again
    at `retry_at` at the earliest.
    """

    def __init__(self, reason: str, *, host: str, retry_at: datetime) -> None:
        super().__init__(reason)
        self.host = host
        self.retry_at = retry_at


class RequestDeadlineError(httpx.ReadTimeout):
    """The whole response took longer than the client's `deadline_s` (a server dripping bytes)."""


class HostTurn(Protocol):
    """One attempt's turn at a host, from `HostGate.acquire`."""

    def start(self, delay_s: float) -> None:
        """The checks passed and the request is sent now: the host's delay counts from here."""

    def cancel(self) -> None:
        """A check refused the attempt: give the turn back, the host's delay is untouched."""

    def done(self) -> None:
        """The request finished or failed: the host is free for its next request."""


class HostGate(Protocol):
    """Decides when a host may be asked again (ADR 0030 § 2)."""

    def acquire(self, host: str, delay_s: float) -> HostTurn:
        """Wait until `host` may be asked, at least `delay_s` after its previous request started.

        A gate that gives up waiting raises `HostBusyError`."""

    def penalize(self, host: str, seconds: float, reason: str) -> None:
        """Back off from `host` for `seconds` (429, 5xx, network errors) before its next request."""


class _MemoryTurn:
    def __init__(self, gate: "InMemoryHostGate", host: str) -> None:
        self._gate = gate
        self._host = host

    def start(self, delay_s: float) -> None:
        self._gate.started(self._host)

    def cancel(self) -> None:
        return None

    def done(self) -> None:
        return None


class InMemoryHostGate:
    """The last request start per exact host in memory; backoffs are slept in the calling thread.

    Right for one process per site and for CLI tools; two clients never see each other's requests.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: dict[str, float] = {}

    def acquire(self, host: str, delay_s: float) -> HostTurn:
        last = self._last_request_at.get(host)
        if last is not None:
            wait = last + delay_s - self._clock()
            if wait > 0:
                self._sleep(wait)
        return _MemoryTurn(self, host)

    def started(self, host: str) -> None:
        self._last_request_at[host] = self._clock()

    def penalize(self, host: str, seconds: float, reason: str) -> None:
        self._sleep(seconds)


def host_key(host_or_url: str) -> str:
    """The slot key of a URL or host: lower-case hostname without port, a leading `www.` removed.

    `www.site22.example` and `site22.example` are one server as far as politeness goes (ADR 0030 § 2);
    a bare `www.tld`-style name with no further dot keeps its `www`.
    """
    value = host_or_url.strip()
    if "://" in value:
        value = _host(value)
    else:
        value = value.lower()
        if value.startswith("["):
            value = value.split("]", 1)[0] + "]"
        elif value.count(":") == 1:
            value = value.split(":", 1)[0]
    value = value.rstrip(".")
    if value.startswith("www.") and value.count(".") >= 2:
        return value[len("www.") :]
    return value


class AddressNotAllowedError(RequestRefusedError):
    """`public_only`: not an http(s) URL, or its host has no address or a non-public one."""


#: `AddressNotAllowedError.reason` when a name has no address at all: DNS trouble, maybe passing,
#: unlike a host that resolves to a non-public address.
HOST_DOES_NOT_RESOLVE = "host does not resolve"


def is_public_address(value: str) -> bool:
    """True only for a globally routable unicast address (IPv4-mapped and NAT64 unwrapped)."""
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(
            value.split("%")[0]
        )
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address in NAT64:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address.is_global and not address.is_multicast


def resolve_host(host: str) -> list[str]:
    """Every address the system resolver returns for `host`; [] when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def public_addresses(host: str, resolver: Resolver = resolve_host) -> list[str]:
    """The addresses to connect to for `host`, all of them public, or `AddressNotAllowedError`.

    One non-public address refuses the host: which record a connection ends up using is not
    something to find out afterwards.
    """
    if not host:
        raise AddressNotAllowedError("no host")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        addresses = resolver(host)
    else:
        addresses = [str(literal)]
    if not addresses:
        raise AddressNotAllowedError(HOST_DOES_NOT_RESOLVE)
    if not all(is_public_address(address) for address in addresses):
        raise AddressNotAllowedError("host resolves to a non-public address")
    return addresses


def check_public_url(url: str, resolver: Resolver = resolve_host) -> None:
    """Refuse anything but http(s) to a host with only public addresses."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError as exc:
        raise AddressNotAllowedError("invalid URL") from exc
    if parts.scheme not in ("http", "https"):
        raise AddressNotAllowedError("not an http(s) URL")
    public_addresses(host, resolver)


class PublicOnlyBackend(httpcore.NetworkBackend):
    """Resolves a host once, refuses non-public addresses, connects only to a checked address."""

    def __init__(
        self, resolver: Resolver = resolve_host, inner: httpcore.NetworkBackend | None = None
    ) -> None:
        self._resolver = resolver
        self._inner = inner if inner is not None else httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        error: Exception = AddressNotAllowedError(HOST_DOES_NOT_RESOLVE)
        for address in public_addresses(host, self._resolver):
            try:
                return self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        raise error

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise AddressNotAllowedError("unix sockets are not allowed")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class PublicOnlyTransport(httpx.HTTPTransport):
    """httpx's own transport with its connection pool on a `PublicOnlyBackend`.

    httpx offers no public way to choose the network backend, so the pool it built is replaced by
    an equivalent one on the checked backend. `core/tests/test_http.py` asserts the backend is in
    place, so an httpx upgrade that changes this fails a test instead of connecting unchecked.
    No proxies (`trust_env=False`): a proxy would resolve the name itself.
    """

    def __init__(self, resolver: Resolver = resolve_host) -> None:
        super().__init__(trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=PublicOnlyBackend(resolver),
        )

    @property
    def network_backend(self) -> httpcore.NetworkBackend | None:
        pool = self._pool
        return pool._network_backend if isinstance(pool, httpcore.ConnectionPool) else None


@dataclass(frozen=True)
class FetchResponse:
    """A fully read HTTP response. Header names are lower-case."""

    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    attempts: int = 1

    @property
    def encoding(self) -> str:
        """The header's charset, else an HTML body's `<meta charset>`, else UTF-8."""
        content_type = self.headers.get("content-type", "")
        for part in content_type.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                if codec := _codec(value.strip().strip('"')):
                    return codec
                break
        media_type = content_type.split(";")[0].strip().lower()
        if media_type in ("", "text/html", "application/xhtml+xml"):
            match = META_CHARSET.search(self.body[:META_PRESCAN_BYTES])
            if match and (codec := _codec(match.group(1).decode("ascii"))):
                return codec
        return "utf-8"

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding, errors="replace")


class PoliteClient:
    """Synchronous HTTP GET client that spaces out requests per host and retries politely."""

    def __init__(
        self,
        user_agent: str,
        *,
        timeout_s: float = 20.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_retries: int = 3,
        backoff_s: float = 2.0,
        max_backoff_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        follow_redirects: bool = True,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        public_only: bool = False,
        resolver: Resolver = resolve_host,
        observer: RequestObserver | None = None,
        label: RequestLabel | None = None,
        gate: HostGate | None = None,
        deadline_s: float | None = None,
    ) -> None:
        """`follow_redirects=False` returns 3xx responses as they are (discovery probes follow
        them by hand). `public_only` checks every hop's address and, unless a test passes its own
        `transport`, connects through `PublicOnlyTransport`. `observer` counts every attempt;
        `label` is the default `RequestLabel` of requests that name none. `gate` decides when a
        host may be asked (default: `InMemoryHostGate` on `clock` and `sleep`). `deadline_s`
        bounds one attempt's whole response, not only each read: `RequestDeadlineError`, never
        retried."""
        if public_only and transport is None:
            transport = PublicOnlyTransport(resolver)
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": ACCEPT},
            timeout=timeout_s,
            follow_redirects=False,
            transport=transport,
        )
        self._follow_redirects = follow_redirects
        self._max_redirects = max_redirects
        self._public_only = public_only
        self._resolver = resolver
        self._max_bytes = max_bytes
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._max_backoff_s = max_backoff_s
        self._clock = clock
        self._observer = observer
        self._label = label or RequestLabel()
        self._gate: HostGate = gate if gate is not None else InMemoryHostGate(clock, sleep)
        self._deadline_s = deadline_s
        self._domain_cookies: dict[str, dict[str, str]] = {}

    @property
    def public_only(self) -> bool:
        return self._public_only

    def set_domain_cookies(self, domain: str, cookies: Mapping[str, str]) -> None:
        """Send `cookies` on requests to `domain` and its subdomains, never to other hosts.

        Replaces the cookies previously set for `domain` through this method (an empty mapping
        removes them); cookies the server set itself are left alone.
        """
        jar_domain = "." + domain.lower().lstrip(".")
        previous = self._domain_cookies.pop(jar_domain, {})
        for name in previous.keys() - cookies.keys():
            with contextlib.suppress(KeyError):
                self._client.cookies.delete(name, domain=jar_domain)
        for name, value in cookies.items():
            self._client.cookies.set(name, value, domain=jar_domain)
        if cookies:
            self._domain_cookies[jar_domain] = dict(cookies)

    def get(
        self,
        url: str,
        *,
        min_delay_s: float = 0.0,
        max_bytes: int | None = None,
        before_request: UrlHook | None = None,
        on_redirect: UrlHook | None = None,
        label: RequestLabel | None = None,
    ) -> FetchResponse:
        """GET `url`, waiting until `min_delay_s` has passed since the last request to its host.

        `max_bytes` overrides the client's size limit for this request; the download is aborted as
        soon as the limit is exceeded. Redirects are followed hop by hop, each hop waiting for its
        own host; the hooks are described in the module docstring. Returns the final response
        (also for 4xx/5xx). Raises `httpx.HTTPError` when the request keeps failing at the network
        level or redirects too often, `ResponseTooLargeError` for oversized bodies,
        `AddressNotAllowedError` for a refused hop with `public_only`, `BudgetExhaustedError` from
        the observer, and whatever a hook raises. `label` overrides the client's default label.
        """
        limit = self._max_bytes if max_bytes is None else max_bytes
        label = label or self._label
        for hop in range(self._max_redirects + 1):
            response = self._get_hop(url, min_delay_s, limit, before_request, label, hop)
            location = response.headers.get("location", "")
            if not self._follow_redirects or response.status not in REDIRECT_STATUSES:
                return response
            if not location:
                return response
            try:
                url = str(httpx.URL(url).join(location))
            except httpx.InvalidURL as exc:
                raise httpx.RemoteProtocolError(
                    "invalid redirect location", request=httpx.Request("GET", url)
                ) from exc
            if on_redirect is not None:
                try:
                    on_redirect(url)
                except Exception:
                    target = RequestAttempt(url, _host(url), label, hop + 1)
                    self._record(target, OUTCOME_REFUSED, 0)
                    raise
        raise httpx.TooManyRedirects(
            f"more than {self._max_redirects} redirects", request=httpx.Request("GET", url)
        )

    def flush(self) -> None:
        """Let the observer persist what it recorded so far (a no-op without one)."""
        if self._observer is not None:
            try:
                self._observer.flush()
            except Exception:
                logger.exception("request accounting flush failed")

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _get_hop(
        self,
        url: str,
        min_delay_s: float,
        limit: int,
        before_request: UrlHook | None,
        label: RequestLabel,
        hop: int,
    ) -> FetchResponse:
        """One URL with retries; every attempt waits its turn and passes the checks first."""
        host = _host(url)
        attempt = 0
        while True:
            attempt += 1
            current = RequestAttempt(url, host, label, hop, attempt)
            turn = self._wait_turn(host, min_delay_s, url, before_request, current)
            try:
                try:
                    response = self._read(url, limit)
                finally:
                    turn.done()
            except ResponseTooLargeError as exc:
                self._record(current, outcome_class(exc.status), exc.bytes_read)
                raise
            except RequestDeadlineError:
                self._record(current, OUTCOME_ERROR, 0)
                raise
            except httpx.TransportError as exc:
                self._record(current, OUTCOME_ERROR, 0)
                if attempt > self._max_retries:
                    raise
                self._gate.penalize(host, self._backoff(attempt, None), type(exc).__name__)
                continue
            self._record(current, outcome_class(response.status), len(response.body))
            if response.status in RETRY_STATUSES and attempt <= self._max_retries:
                backoff = self._backoff(attempt, response.headers.get("retry-after"))
                self._gate.penalize(host, backoff, f"HTTP {response.status}")
                continue
            return replace(response, attempts=attempt)

    def _wait_turn(
        self,
        host: str,
        min_delay_s: float,
        url: str = "",
        before_request: UrlHook | None = None,
        attempt: RequestAttempt | None = None,
    ) -> HostTurn:
        """Wait for `host`'s turn, then run the checks; a refusal gives the turn back untouched.

        The observer's budget check runs last: a request the media deny or `public_only` refuses
        is counted as refused by that check and never looks at a budget.
        """
        turn = self._gate.acquire(host, min_delay_s)
        attempt = attempt or RequestAttempt(url, host, self._label)
        try:
            if before_request is not None:
                before_request(url)
            if self._public_only:
                check_public_url(url, self._resolver)
            if self._observer is not None:
                self._observer.admit(attempt)
        except Exception:
            turn.cancel()
            self._record(attempt, OUTCOME_REFUSED, 0)
            raise
        try:
            turn.start(min_delay_s)
        except BaseException:
            turn.cancel()
            raise
        return turn

    def _record(self, attempt: RequestAttempt, outcome: str, size: int) -> None:
        """Report one attempt; accounting trouble never fails the request itself."""
        if self._observer is None:
            return
        try:
            self._observer.record(attempt, outcome, size)
        except Exception:
            logger.exception("request accounting failed for %s", attempt.host)

    def _read(self, url: str, max_bytes: int) -> FetchResponse:
        started = self._clock()
        with self._client.stream("GET", url) as response:
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise ResponseTooLargeError(
                    f"{url}: {declared} bytes > {max_bytes}", status=response.status_code
                )
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                if self._deadline_s is not None and self._clock() - started > self._deadline_s:
                    raise RequestDeadlineError(
                        f"{url}: no complete response within {self._deadline_s:g} s",
                        request=response.request,
                    )
                size += len(chunk)
                if size > max_bytes:
                    raise ResponseTooLargeError(
                        f"{url}: more than {max_bytes} bytes",
                        status=response.status_code,
                        bytes_read=size,
                    )
                chunks.append(chunk)
            return FetchResponse(
                url=str(response.url),
                status=response.status_code,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=b"".join(chunks),
            )

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        seconds = _retry_after_s(retry_after)
        if seconds is None:
            seconds = self._backoff_s * 2 ** (attempt - 1)
        return min(seconds, self._max_backoff_s)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _codec(label: str) -> str | None:
    try:
        return codecs.lookup(label).name
    except LookupError:
        return None


def _retry_after_s(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)
