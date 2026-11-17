"""How much we watch, as a customer may learn it: a coarse bucket, never a count, never a name.

Operator decision (2026-09-16, docs/customer-ui-plan.md §8 question 1): the list of sites we crawl
is not shown to customers, but "no re-upload anywhere"
is only as true as that coverage, so the customer is told its rough size. The bucket is the floor
of the number of enabled sites (17 is "10+"), and it is the same for every organisation: it says
nothing about anybody's data.

Only the bucket code leaves this module. An exact count for long-standing subscribers is a
possible later bonus and deliberately not built.
"""

from django.conf import settings

from catalog.models import Site
from marceline.accounts_replica import replicated_monitored_sources
from marceline.app.schemas import MonitoredSources

#: Lower bounds, largest first. Below the smallest one the answer is `fewer_than_10`.
BUCKETS: tuple[tuple[int, MonitoredSources], ...] = (
    (1000, "1000_plus"),
    (500, "500_plus"),
    (250, "250_plus"),
    (100, "100_plus"),
    (50, "50_plus"),
    (10, "10_plus"),
)


def bucket_for(count: int) -> MonitoredSources:
    """The floor bucket for a number of monitored sources."""
    for floor, bucket in BUCKETS:
        if count >= floor:
            return bucket
    return "fewer_than_10"


def monitored_sources() -> MonitoredSources:
    """The bucket for the sites currently enabled for crawling.

    Production has no catalog (ADR 0028): there the bucket is the one development computed, carried
    by the replicated accounts document (`marceline/accounts_replica.py`).
    """
    if settings.MARCELINE_ROLE == "prod":
        return replicated_monitored_sources() or bucket_for(0)
    return bucket_for(Site.objects.filter(enabled=True).count())
