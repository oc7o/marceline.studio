"""On production, customer data is answered from the signed cache, never from local tables.

`MARCELINE_ROLE=prod` (ADR 0028). Production's database holds accounts, sessions and the cache; it
has no originals, matches or pages. So the customer read endpoints that show an organisation's
data — `originals`, `originals/{id}`, `findings`, `cases`, `cases/{id}`, `removals`, `rights`
(ADR 0034) — are answered here, from the organisation's bundle (`delivery.store`), with the exact
status, content type and body development rendered, plus `X-Data-As-Of` (when development rendered
it).

The routes are matched by their URL *pattern* after resolving the path, so any spelling that
reaches one of those views is served from the cache. Everything else passes through untouched:
`me` and `auth/*` are production's own (local accounts), an anonymous caller gets the view's own
401, and a signed-in account without an organisation owns nothing, which the views answer without
reading any customer data. Detail routes are keyed by the original's public id (ADR 0033), exactly
as the URL spells it; an id that is not a well-formed public id — an old integer URL, a typo — is
answered from the bundle's `*` document, the same 404 development gives, without a local read.

No servable bundle — a cold cache while development is down, or data older than 72 hours — is a
**503 `data_temporarily_unavailable`**, never an empty list: an empty list would read as "nothing
found".
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import Resolver404, resolve
from django.utils import timezone

from core import public_ids
from delivery import store
from marceline.app.ownership import ORIGINAL_ID_PREFIX, membership_of
from marceline.notices.models import NOTICE_ID_PREFIX

logger = logging.getLogger("marceline.cache")

#: URL pattern → (document family, the path parameter holding the id, or None for a list view).
#: The notice documents (ADR 0046) join the same table; `notices/candidates` is a detail family
#: whose key is an original's public id, `notices` one whose key is a draft's.
CACHED_ROUTES: dict[str, tuple[str, str | None]] = {
    "api/v1/app/originals": ("originals", None),
    "api/v1/app/findings": ("findings", None),
    "api/v1/app/cases": ("cases", None),
    "api/v1/app/removals": ("removals", None),
    "api/v1/app/rights": ("rights", None),
    "api/v1/app/notices": ("notices", None),
    # Account-control verification (ADR 0055): one document per organisation, like `rights`.
    "api/v1/app/verification": ("verification", None),
    # The creator's own data requests (ADR 0056): one document per organisation, like `rights`.
    # A cached read here is what lets the public host show a creator that their request is on
    # record at all — the rows live on development, where somebody answers them.
    "api/v1/app/data-requests": ("data-requests", None),
    "api/v1/app/originals/<original_id>": ("originals", "original_id"),
    "api/v1/app/cases/<original_id>": ("cases", "original_id"),
    "api/v1/app/notices/<notice_id>": ("notices", "notice_id"),
    "api/v1/app/notices/candidates/<original_id>": ("notices/candidates", "original_id"),
}
#: Which kind of public id each path parameter carries. A value of another kind — an old integer
#: URL, a typo, a draft id where an original id belongs — is answered from the family's `*`
#: document, the same 404 development gives, without a local read.
ID_PREFIXES: dict[str, str] = {"original_id": ORIGINAL_ID_PREFIX, "notice_id": NOTICE_ID_PREFIX}
UNAVAILABLE = "data_temporarily_unavailable"
RETRY_AFTER_S = 60

#: The clock the cache rules run on; tests replace it.
clock: Callable[[], datetime] = timezone.now


def document_key(route: str, kwargs: dict[str, object]) -> tuple[str, str | None] | None:
    """The bundle key (and its 404 fallback) for a resolved route, or None to pass through."""
    entry = CACHED_ROUTES.get(route)
    if entry is None:
        return None
    family, parameter = entry
    if parameter is None:
        return family, None
    value = kwargs.get(parameter, "")
    if not public_ids.is_valid(ID_PREFIXES[parameter], value):
        # Not a public id of that kind at all (an integer, a typo): development answers it with
        # the same 404 as an unknown id, and so does the bundle's `*` document.
        return f"{family}/*", None
    return f"{family}/{value}", f"{family}/*"


def as_of_header(moment: datetime) -> str:
    """ISO 8601 in UTC, e.g. `2026-09-17T10:15:00Z`: when development rendered the data."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def unavailable_response() -> HttpResponse:
    """Nothing servable: 503, never an empty answer. Also used by the notice download, which the
    view serves from the bundle itself (ADR 0046)."""
    response: HttpResponse = JsonResponse({"detail": UNAVAILABLE}, status=503)
    response["Retry-After"] = str(RETRY_AFTER_S)
    return response


class CustomerCacheMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if settings.MARCELINE_ROLE != "prod" or request.method != "GET":
            return self.get_response(request)
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return self.get_response(request)
        keys = document_key(match.route, match.kwargs)
        if keys is None or not request.user.is_authenticated:
            return self.get_response(request)
        membership = membership_of(request.user)
        if membership is None:
            return self.get_response(request)
        key, fallback = keys
        try:
            document = store.document_for(
                membership.organisation_id, key, fallback_key=fallback, now=clock()
            )
        except store.Unavailable as exc:
            logger.warning(
                "customer data unavailable for organisation %s: %s",
                membership.organisation_id,
                exc.code,
            )
            return unavailable_response()
        response = HttpResponse(
            document.body.encode(), status=document.status, content_type=document.content_type
        )
        response["X-Data-As-Of"] = as_of_header(document.as_of)
        return response
