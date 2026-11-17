"""An admin-list `Paginator` that reads Postgres's own row estimate instead of scanning the
table (issue: the changelist's "(N total)" figure cost a full `COUNT(*)` — 33 ms / 7,292 buffers
on `crawler_crawltask` alone — on every list load, filtered or not).

`pg_class.reltuples` is the planner's own estimate of a table's row count, kept current by
autovacuum/autoanalyze rather than by every write. The operator's own framing: "I wouldn't do a
full table scan every time. Do we store column metadata or so?" — Postgres already does, and
reading it is a single indexed lookup instead of a sequential (or index-only) scan of the table.

A *filtered* count has no such shortcut — Postgres keeps no estimate for an arbitrary WHERE
clause — so a filtered queryset is always counted exactly; only a bare, unfiltered "how many rows
does this table have" swaps to the estimate.
"""

from typing import Any

from django.core.paginator import Paginator
from django.db import connections
from django.db.models import QuerySet
from django.utils.functional import cached_property

#: Below this estimate — or when Postgres has never analyzed the table (`reltuples` negative) —
#: the table is small enough that an exact `COUNT(*)` is cheap, and exactness there is free.
ESTIMATE_THRESHOLD = 1_000


class EstimatedCount(int):
    """An `int` that renders with a leading "~", so an estimated total never looks like an exact
    one. It behaves exactly like `int` everywhere else — comparisons, pagination arithmetic,
    `{% blocktranslate count %}` pluralization — only its string form differs, which is enough to
    change what Django's admin templates print without forking any of them.
    """

    def __str__(self) -> str:
        return f"~{int(self):,}"

    def __repr__(self) -> str:
        return f"EstimatedCount({int(self)})"


class EstimatedCountPaginator(Paginator[Any]):
    """A `Paginator` whose `count` is Postgres's own planner estimate for an *unfiltered*
    queryset (`pg_class.reltuples`), falling back to an exact `COUNT(*)` for a filtered queryset
    or a small/never-analyzed table.

    `is_estimated` records which branch the last `count` access took, for a caller that wants to
    know without re-deriving it from the value's type.
    """

    is_estimated: bool = False

    @cached_property
    def count(self) -> int:
        queryset = self.object_list
        if isinstance(queryset, QuerySet):
            if not queryset.query.where:
                estimate = _table_estimate(queryset)
                if estimate is not None and estimate >= ESTIMATE_THRESHOLD:
                    self.is_estimated = True
                    return EstimatedCount(estimate)
            self.is_estimated = False
            return int(queryset.count())
        self.is_estimated = False
        return len(self.object_list)


def _table_estimate(queryset: QuerySet[Any]) -> int | None:
    """Postgres's own row estimate for `queryset`'s table, or `None` if it has never been
    analyzed (`reltuples` negative) or the table cannot be resolved."""
    db_table = queryset.model._meta.db_table
    connection = connections[queryset.db]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass", [db_table]
        )
        row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    estimate = int(row[0])
    return estimate if estimate >= 0 else None
