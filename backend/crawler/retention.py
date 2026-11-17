"""Retention for the discovery search trail (ADR 0048, issue #103).

A `crawler.SearchCall` is two records in one row. It is the **money ledger** — ADR 0013's spend
guard sums `cost_usd` over a rolling 24 h window before every paid request, and the admin and the
staff discovery API show the lifetime total — and it is a record that **this named person was
searched for in this context**, because a query is a name or an alias in one of ADR 0013's
templates. The search request is the one place in normal operation where a name leaves the house.

So the row is never deleted and the admin's "no delete" stays; the **query text is what expires**:
after `SEARCH_QUERY_RETENTION_DAYS` it is set to `""` and `query_scrubbed_at` is stamped, and
engine, mode, location, language, result count, cost, status, error and timestamp stay exactly as
they were. Scrubbing the text *is* the deletion of the personal part; what is left is an accounting
line.

The same text was copied into `SourceCandidate.evidence["queries"][*]["query"]`, so the same pass
scrubs that key for every candidate untouched for the same period (`last_seen_at`). The sample's
`rank`, `url` and `title` stay: they are the evidence that this domain carries copies, and a result
title is the pirate site's own words — the same category as `Page.title`, which suppression keeps on
purpose. A candidate that link mining keeps touching keeps its samples while that review lasts.

`crawler.CandidateCreator` — the pointer from a candidate to the creator whose name prompted it —
is deliberately **not** touched here: it is two integers, it is what `distinct_creators` is
recomputed from, and it is `CASCADE` on `Creator`, so ADR 0048's name pass takes it with the name.

`marceline.suppression` calls `scrub_search_calls` too, on the rows whose text named one person,
instead of deleting them as it used to: the name goes exactly as before and the accounting no longer
moves.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from core import deletion_journal
from crawler.models import SearchCall, SourceCandidate

logger = logging.getLogger(__name__)

#: Days a discovery query's text is kept. A query has no use once its candidates are recorded, and
#: they are recorded in the same transaction; this is one candidate-review cycle, no more.
SEARCH_QUERY_RETENTION_DAYS = 30

#: The key inside `SourceCandidate.evidence["queries"][*]` that holds the query text.
EVIDENCE_QUERY_KEY = "query"


def scrubbed_sample(sample: Any) -> tuple[Any, bool]:
    """One evidence sample without its query text, and whether anything changed. Pure.

    A sample that is not a mapping, or that carries no `query`, is returned unchanged: this only
    ever removes one key, never rewrites a rank, a URL or a result title.
    """
    if not isinstance(sample, Mapping) or not sample.get(EVIDENCE_QUERY_KEY):
        return sample, False
    return {key: value for key, value in sample.items() if key != EVIDENCE_QUERY_KEY}, True


def scrubbed_evidence(evidence: Any) -> tuple[Any, int]:
    """`evidence` with the query text removed from every sample, and how many samples changed.

    Pure. Everything else in the document — `linking_pages`, `research`, `mirror_of`, counts — is
    returned untouched.
    """
    if not isinstance(evidence, Mapping):
        return evidence, 0
    samples = evidence.get("queries")
    if not isinstance(samples, list):
        return evidence, 0
    scrubbed = []
    changed = 0
    for sample in samples:
        result, did = scrubbed_sample(sample)
        scrubbed.append(result)
        changed += did
    if not changed:
        return evidence, 0
    return {**evidence, "queries": scrubbed}, changed


def scrub_search_calls(call_ids: Sequence[int], *, now: datetime | None = None) -> int:
    """Blank the query text of these calls, keeping the row and its cost. Returns rows changed.

    Idempotent: a row already scrubbed is not selected again (`query=""` is excluded), so the
    stamp is written once, and re-running changes nothing.
    """
    ids = list(call_ids)
    if not ids:
        return 0
    scrubbing = SearchCall.objects.filter(pk__in=ids).exclude(query="")
    # ADR 0057: a restore from before the scrub brings the query text — a name in an ADR 0013
    # template — back into the ledger row. One journal line per call actually scrubbed; the id
    # only, never the text.
    deletion_journal.record_many(
        "search_query_scrubbed",
        ({"call": pk} for pk in scrubbing.order_by("pk").values_list("pk", flat=True)),
        now=now,
    )
    return scrubbing.update(query="", query_scrubbed_at=now or timezone.now())


@dataclass
class QueryRetentionReport:
    dry_run: bool
    now: datetime
    retain_days: int
    scrub_before: datetime
    #: Calls that still carry a query text at all, any age.
    with_text: int
    #: Calls due: older than the period and not scrubbed yet.
    due: int
    #: Calls actually scrubbed (a real run; equals `due` unless something raced the pass).
    scrubbed: int = 0
    #: Candidates whose evidence samples still hold query text, and those due / scrubbed.
    candidates_with_text: int = 0
    candidates_due: int = 0
    candidates_scrubbed: int = 0
    samples_scrubbed: int = 0
    #: Candidate ids whose samples were (or would be) scrubbed. Ids only, never a domain.
    candidate_ids: list[int] = field(default_factory=list)


def apply_query_retention(
    *,
    now: datetime | None = None,
    dry_run: bool = True,
    retain_days: int = SEARCH_QUERY_RETENTION_DAYS,
) -> QueryRetentionReport:
    """Scrub discovery query text older than `retain_days`. Idempotent; `dry_run` only counts.

    Deletes no row, touches no cost and never reports a query, a name or a domain.
    """
    now = now or timezone.now()
    scrub_before = now - timedelta(days=retain_days)
    with_text = SearchCall.objects.exclude(query="").count()
    due_ids = list(
        SearchCall.objects.exclude(query="")
        .filter(created_at__lte=scrub_before)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    candidates: list[tuple[int, Any, int]] = []
    candidates_with_text = 0
    for pk, evidence, last_seen_at in (
        SourceCandidate.objects.exclude(evidence={})
        .order_by("pk")
        .values_list("pk", "evidence", "last_seen_at")
    ):
        cleaned, changed = scrubbed_evidence(evidence)
        if not changed:
            continue
        candidates_with_text += 1
        if last_seen_at is not None and last_seen_at <= scrub_before:
            candidates.append((pk, cleaned, changed))
    report = QueryRetentionReport(
        dry_run=dry_run,
        now=now,
        retain_days=retain_days,
        scrub_before=scrub_before,
        with_text=with_text,
        due=len(due_ids),
        candidates_with_text=candidates_with_text,
        candidates_due=len(candidates),
        candidate_ids=[pk for pk, _, _ in candidates],
    )
    if dry_run:
        return report
    with transaction.atomic():
        report.scrubbed = scrub_search_calls(due_ids, now=now)
        for pk, cleaned, changed in candidates:
            # ADR 0057: the same text, copied into the candidate's evidence. The candidate id only.
            deletion_journal.record("candidate_queries_scrubbed", candidate=pk, now=now)
            SourceCandidate.objects.filter(pk=pk).update(evidence=cleaned)
            report.candidates_scrubbed += 1
            report.samples_scrubbed += changed
    if report.scrubbed or report.candidates_scrubbed:
        # Counters only: the whole point of the pass is that this text is not repeated anywhere.
        logger.info(
            "QUERY RETENTION scrubbed %s search call(s) older than %s and %s candidate(s) "
            "(%s evidence sample(s))",
            report.scrubbed,
            scrub_before.date().isoformat(),
            report.candidates_scrubbed,
            report.samples_scrubbed,
        )
    return report
