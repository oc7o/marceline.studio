from django.db import connection
from django.utils import timezone

from core.models import HostSlot
from core.pagination import ESTIMATE_THRESHOLD, EstimatedCount, EstimatedCountPaginator


def _make_host_slots(n: int) -> None:
    now = timezone.now()
    HostSlot.objects.bulk_create(
        HostSlot(key=f"host-{i}.example", next_allowed_at=now) for i in range(n)
    )


def _analyze(table: str) -> None:
    # `ANALYZE` is allowed inside a transaction block (unlike `VACUUM`), and its write to
    # `pg_class` is visible to later statements of this same test's transaction.
    with connection.cursor() as cursor:
        cursor.execute(f"ANALYZE {table}")


def test_filtered_queryset_always_counts_exactly(db: None) -> None:
    """A WHERE clause has no Postgres estimate, so it is always counted exactly — however many
    rows the table's statistics say it has."""
    _make_host_slots(ESTIMATE_THRESHOLD + 50)
    _analyze("core_hostslot")

    queryset = HostSlot.objects.filter(min_delay_s__gte=0.0)
    paginator = EstimatedCountPaginator(queryset, per_page=50)

    assert paginator.count == ESTIMATE_THRESHOLD + 50
    assert type(paginator.count) is int
    assert paginator.is_estimated is False


def test_unfiltered_large_table_uses_the_planner_estimate(db: None) -> None:
    """An unfiltered queryset over a table Postgres has analyzed, and that is large enough that
    the estimate is trusted, reads `pg_class.reltuples` instead of counting."""
    _make_host_slots(ESTIMATE_THRESHOLD + 500)
    _analyze("core_hostslot")

    queryset = HostSlot.objects.all()
    paginator = EstimatedCountPaginator(queryset, per_page=50)

    # `ANALYZE` on a table this size (well under its default statistics sample) scans it in
    # full, so the estimate is exact here — but it is still read as an estimate, not a count.
    assert paginator.count == ESTIMATE_THRESHOLD + 500
    assert paginator.is_estimated is True
    assert isinstance(paginator.count, EstimatedCount)
    assert str(paginator.count) == f"~{ESTIMATE_THRESHOLD + 500:,}"


def test_unfiltered_tiny_table_counts_exactly(db: None) -> None:
    """A table small enough that its estimate falls below the threshold is counted exactly —
    exactness there is free, and a stale estimate isn't worth trusting."""
    _make_host_slots(3)
    _analyze("core_hostslot")

    queryset = HostSlot.objects.all()
    paginator = EstimatedCountPaginator(queryset, per_page=50)

    assert paginator.count == 3
    assert type(paginator.count) is int
    assert paginator.is_estimated is False


def test_estimated_count_renders_with_a_tilde_but_behaves_like_an_int() -> None:
    value = EstimatedCount(86_000)

    assert str(value) == "~86,000"
    assert value == 86_000
    assert value + 1 == 86_001
    assert value <= 100_000
