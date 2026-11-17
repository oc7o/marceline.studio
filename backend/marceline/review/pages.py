"""Page-level actions shared by the queues: targeted availability rechecks.

Only queues crawl tasks (`crawler.services.recheck_page_ids`); the per-site crawler fetches them
under robots.txt, the per-host delay and the daily budget.
"""

from collections.abc import Iterable

from django.http import HttpRequest
from ninja import Router, Status

from crawler.services import recheck_page_ids
from index.models import Page
from marceline.review.auth import staff_auth
from marceline.review.schemas import ErrorOut, IdsIn, RecheckOut

router = Router(tags=["review"], auth=staff_auth)


def queue_recheck(page_ids: Iterable[int]) -> RecheckOut:
    result = recheck_page_ids(page_ids)
    return RecheckOut(queued=result.queued, in_progress=result.in_progress, skipped=result.skipped)


@router.post("/recheck", response={200: RecheckOut, 404: ErrorOut})
def recheck(request: HttpRequest, payload: IdsIn) -> Status[RecheckOut | ErrorOut]:
    """Queue availability checks for these pages at recheck priority."""
    known = set(Page.objects.filter(pk__in=payload.ids).values_list("pk", flat=True))
    missing = sorted(set(payload.ids) - known)
    if missing:
        return Status(404, ErrorOut(detail=f"Unknown page ids: {', '.join(map(str, missing))}"))
    return Status(200, queue_recheck(known))
