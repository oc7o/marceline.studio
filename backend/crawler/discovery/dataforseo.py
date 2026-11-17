"""The sole direct-httpx exception: authenticated search API, never site crawling."""

import base64
import re
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from catalog.request_accounting import SEARCH_API, record_request
from crawler.models import SearchCall

DEFAULT_LOCATION_CODE = 2840
# Conservative estimates per depth-10 search; verify provider pricing before production.
MODE_PRICE_USD = {"live": Decimal("0.002"), "standard": Decimal("0.0006")}
API_ROOT = "https://api.dataforseo.com/v3/serp/google/organic"
MAX_READY_POLLS = 10
SPEND_LOCK_ID = 73130013
MAX_ERROR_LENGTH = 1000
# 40101: DataForSEO auth/temporary provider error, retried rather than ending the run.
RETRYABLE_PROVIDER_CODE = 40101
RETRY_DELAYS_S = (1.0, 2.0)


class DiscoveryError(Exception):
    pass


class SpendLimit(DiscoveryError):
    pass


class ProviderError(DiscoveryError):
    def __init__(self, message: str, code: int | None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def stops_run(self) -> bool:
        return self.code is not None and 40100 <= self.code <= 40299


def provider_error(body: dict[str, Any]) -> ProviderError | None:
    failure = (
        body
        if body.get("status_code") != 20000
        else next(
            (
                task
                for task in (body.get("tasks") or [])
                if task.get("status_code") not in {20000, 20100}
            ),
            None,
        )
    )
    if failure is None or "status_code" not in failure:
        return None
    raw_code = failure["status_code"]
    code = raw_code if isinstance(raw_code, int) else None
    message = str(failure.get("status_message") or "")
    message = re.sub(
        r"""(?ix)["']?(?:authorization|login|password|api[_-]?key|token)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+(?:\s+[^\s,;]+)?)""",
        "[redacted]",
        message,
    )
    message = re.sub(r"(?i)\b(?:Basic|Bearer)\s+[^\s,;]+", "[redacted]", message)
    credentials = f"{settings.DATAFORSEO_LOGIN}:{settings.DATAFORSEO_PASSWORD}"
    for secret in (
        base64.b64encode(credentials.encode()).decode(),
        settings.DATAFORSEO_LOGIN,
        settings.DATAFORSEO_PASSWORD,
    ):
        message = message.replace(secret, "[redacted]")
    message = " ".join(message.split())
    return ProviderError(f"Provider error {code}: {message}"[:MAX_ERROR_LENGTH], code)


class DataForSEO:
    def __init__(
        self,
        mode: str = "live",
        max_calls: int | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        location_code: int | None = None,
        language: str | None = None,
    ) -> None:
        """`location_code` / `language` default to `DISCOVERY_LOCATION_CODE` / `_LANGUAGE`."""
        if not settings.DATAFORSEO_LOGIN or not settings.DATAFORSEO_PASSWORD:
            raise DiscoveryError("Set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD for discovery.")
        if mode not in MODE_PRICE_USD:
            raise DiscoveryError("Unsupported discovery mode.")
        self.mode = mode
        self.location_code: int = (
            location_code if location_code is not None else settings.DISCOVERY_LOCATION_CODE
        )
        self.language: str = language or settings.DISCOVERY_LANGUAGE
        self.limit = min(
            max_calls if max_calls is not None else settings.DISCOVERY_MAX_CALLS_PER_RUN,
            settings.DISCOVERY_MAX_CALLS_PER_RUN,
        )
        self.calls = 0
        self.cost = Decimal(0)
        self.transport = transport

    def _request(
        self,
        method: str,
        path: str,
        query: str,
        payload: list[dict[str, Any]] | None = None,
        estimate: Decimal = Decimal(0),
    ) -> dict[str, Any]:
        # Bounded retry for the provider's transient/auth error (40101) only; every attempt is
        # a normal call through _send, so it still goes through the spend guard and call cap.
        delays = iter(RETRY_DELAYS_S)
        while True:
            try:
                return self._send(method, path, query, payload, estimate)
            except ProviderError as exc:
                delay = next(delays, None)
                if exc.code != RETRYABLE_PROVIDER_CODE or delay is None:
                    raise
                time.sleep(delay)

    def _send(
        self,
        method: str,
        path: str,
        query: str,
        payload: list[dict[str, Any]] | None = None,
        estimate: Decimal = Decimal(0),
    ) -> dict[str, Any]:
        # Serialize the check and paid request across concurrent discovery runs.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SPEND_LOCK_ID])
            spent = SearchCall.objects.filter(
                created_at__gte=timezone.now() - timedelta(hours=24)
            ).aggregate(total=Sum("cost_usd"))["total"] or Decimal(0)
            if self.calls >= self.limit or spent + estimate > Decimal(
                str(settings.DISCOVERY_DAILY_BUDGET_USD)
            ):
                raise SpendLimit("Discovery spend/call limit reached.")
            log = SearchCall.objects.create(
                query=query,
                mode=self.mode,
                location=self.location_code,
                language=self.language,
                status="pending",
            )
            self.calls += 1
            data: dict[str, Any] = {}
            error = ""
            failure: ProviderError | None = None
            try:
                # A paid API with credentials, so not PoliteClient; counted all the same (#74).
                try:
                    with httpx.Client(
                        auth=(settings.DATAFORSEO_LOGIN, settings.DATAFORSEO_PASSWORD),
                        timeout=30,
                        transport=self.transport,
                        follow_redirects=False,
                    ) as client:
                        response = client.request(method, API_ROOT + path, json=payload)
                except httpx.HTTPError:
                    record_request(API_ROOT + path, kind=SEARCH_API, status=None)
                    raise
                record_request(
                    API_ROOT + path,
                    kind=SEARCH_API,
                    status=response.status_code,
                    size=len(response.content),
                )
                try:
                    raw = response.json()
                except ValueError:
                    raw = None
                if isinstance(raw, dict):
                    data = raw
                    failure = provider_error(data)
                if failure:
                    error = str(failure)
                elif not response.is_success:
                    error = f"HTTP {response.status_code}"
                elif not isinstance(raw, dict):
                    error = "Invalid response"
                log.cost_usd = Decimal(str(data.get("cost", 0)))
                if not log.cost_usd.is_finite() or log.cost_usd < 0:
                    log.cost_usd = Decimal(0)
                    raise ValueError("Invalid cost")
                if not error:
                    log.result_count = len(organic_items(data))
            except (
                httpx.HTTPError,
                ValueError,
                TypeError,
                ArithmeticError,
                KeyError,
                AttributeError,
            ) as exc:
                # Do not log exception text: request/response content can contain credentials.
                error = error or type(exc).__name__
            log.status = "error" if error else "ok"
            log.error = error
            log.save()
            self.cost += log.cost_usd
        # Raise outside atomic so failures remain in the audit log.
        if error:
            raise failure or DiscoveryError(error)
        return data

    def search(self, query: str) -> list[dict[str, Any]]:
        payload = [
            {
                "keyword": query,
                "location_code": self.location_code,
                "language_code": self.language,
                "depth": 10,
            }
        ]
        data = self._request(
            "POST",
            "/live/regular" if self.mode == "live" else "/task_post",
            query,
            payload,
            MODE_PRICE_USD[self.mode],
        )
        if self.mode == "live":
            return organic_items(data)
        tasks = data.get("tasks", [])
        if not tasks or not tasks[0].get("id"):
            raise DiscoveryError("Provider returned no task ID.")
        task_id = str(tasks[0]["id"])
        for attempt in range(MAX_READY_POLLS):
            ready = self._request("GET", "/tasks_ready", query)
            if any(
                str(item.get("id")) == task_id
                for task in ready.get("tasks", [])
                for item in (task.get("result") or [])
            ):
                return organic_items(self._request("GET", "/task_get/regular/" + task_id, query))
            if attempt + 1 < MAX_READY_POLLS:
                time.sleep(1)
        raise DiscoveryError("Standard task is not ready; task ID: " + task_id)


def organic_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "rank": int(item["rank_absolute"]),
            "url": str(item["url"]),
            "domain": str(item.get("domain", "")),
            "title": str(item.get("title", "")),
        }
        for task in data.get("tasks", [])
        for result in (task.get("result") or [])
        for item in (result.get("items") or [])
        if item.get("type") == "organic" and item.get("url") and item.get("rank_absolute")
    ]
