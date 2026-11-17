"""Nothing a customer endpoint answers may be stored by a cache.

Every `/api/v1/app/…` answer belongs to exactly one account: the originals of one organisation, its
cases, its invoices, its rights record, who is signed in. None of it carried a `Cache-Control`
header — only the invoice PDF set one for itself — and a response with no freshness information at
all is *heuristically cacheable* (RFC 9111 §4.2.2). `Vary: Cookie` from `SessionMiddleware` makes a
correct shared cache key on the session, which is why this has never leaked anything; but the
guarantee then rests on every intermediary along the way being correct, for answers that must never
be stored at all. A reverse proxy, a corporate middlebox or a CDN put in front of the app later is
not something ADR 0028's tenancy rule should depend on.

So: one header, set on every answer of the customer API, including the ones
`marceline.customer_cache` serves from the signed bundle on production. `private` says the answer
belongs to one user; `no-store` says not to keep it anywhere. The endpoints that already set their
own directive (the invoice PDF's `private, no-store`) keep it — this never overwrites.

Not applied to `/api/v1/health`, which is public, constant and worth caching, nor to the staff
surfaces, which are Tailscale-only and are the admin's own business.
"""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from marceline.public_surface import CUSTOMER_PREFIXES

#: What a customer answer says about itself: it belongs to one account, and no cache may keep it.
NO_STORE = "private, no-store"


class CustomerNoStoreMiddleware:
    """`Cache-Control: private, no-store` on every customer API answer that sets none itself."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.path_info.startswith(CUSTOMER_PREFIXES) and not response.has_header(
            "Cache-Control"
        ):
            response["Cache-Control"] = NO_STORE
        return response
