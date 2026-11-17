"""What an owner switch does to the previous owner's data (ADR 0045, issue #85).

`marceline.app.ownership.reassign` is the only writer of ownership (ADR 0031), so this is the only
place an original changes hands — CLI, admin and `seed_test_customer` alike. Two effects, and the
reason they happen at different times is the whole decision:

- **the case file does not travel, and cannot wait.** `matching.LeakCase` and its `CaseFinding`
  rows hang off the `Reference`, which moves; the moment the move commits, the *new* owner's
  customer view reads the previous owner's `opened_at`, reopen history and per-finding
  `verified_at`/`removed_at` stamps. So they are deleted in the same transaction. The next
  `sync_cases` rebuilds a case for the new owner out of the match rows as they stand — our own
  observations about the work, which are not the previous customer's;
- **everything else takes the window.** The previous owner's notice drafts, rights record, refused
  claims and public id for that file are scoped to their organisation and visible to nobody else,
  so they are only *marked* (`OriginalRemoval`, reason `owner_changed` or `unassigned`) and deleted
  by `retention.py` after `CUSTOMER_DATA_RETENTION_DAYS`. That window is what makes "we offer an
  export first" true.

This module deliberately imports no ownership code: `ownership` imports it, not the other way
round. It takes plain (organisation, sha256) pairs.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from core import deletion_journal
from marceline.deletion.models import OriginalRemoval
from matching.models import LeakCase, Reference

logger = logging.getLogger("marceline.deletion")


@dataclass(frozen=True)
class HandoverEffect:
    """What one file losing an owner costs that owner, and what the move resets for everyone."""

    #: The organisation that is losing this file.
    organisation_id: int
    organisation_name: str
    sha256: str
    #: `unassigned` when the file now belongs to nobody, `owner_changed` when it moved.
    reason: str
    #: Case rows the move resets (they belong to no organisation once the file changes hands).
    cases: int
    findings: int
    #: True when this organisation already had a pending removal mark for the file.
    already_marked: bool


def plan(losses: Iterable[tuple[int, str, str, bool]]) -> list[HandoverEffect]:
    """Effects of these losses, written down. Reads only; safe in a dry run.

    `losses` are `(organisation_id, organisation_name, sha256, now_unassigned)` tuples, one per
    file an organisation is losing.
    """
    effects = []
    for organisation_id, organisation_name, sha256, unassigned in sorted(set(losses)):
        references = list(
            Reference.objects.filter(sha256=sha256).values_list("pk", flat=True).order_by("pk")
        )
        cases = LeakCase.objects.filter(reference_id__in=references)
        effects.append(
            HandoverEffect(
                organisation_id=organisation_id,
                organisation_name=organisation_name,
                sha256=sha256,
                reason=(
                    OriginalRemoval.Reason.UNASSIGNED
                    if unassigned
                    else OriginalRemoval.Reason.OWNER_CHANGED
                ),
                cases=cases.count(),
                findings=sum(case.findings.count() for case in cases),
                already_marked=OriginalRemoval.objects.filter(
                    organisation_id=organisation_id, sha256=sha256, deleted_at__isnull=True
                ).exists(),
            )
        )
    return effects


@transaction.atomic
def apply(effects: Sequence[HandoverEffect], *, now: datetime | None = None) -> None:
    """Reset the case files and mark the previous owners' data for removal.

    Idempotent: a file already marked for this organisation keeps its original `removed_at`, so
    moving an original twice inside the window does not restart its clock. Called from inside
    `reassign`'s transaction; never on a dry run.
    """
    moment = now or timezone.now()
    for effect in effects:
        references = list(
            Reference.objects.filter(sha256=effect.sha256).values_list("pk", flat=True)
        )
        # ADR 0057: a restore from before the switch brings the previous owner's case rows back
        # under the new owner's reference, which is exactly what this deletion exists to prevent.
        # The journal carries the losing organisation's id and the file's sha256, nothing else.
        if effect.cases:
            deletion_journal.record(
                "case_file_reset",
                organisation=effect.organisation_id,
                sha256=effect.sha256,
                now=moment,
            )
        deleted, _ = LeakCase.objects.filter(reference_id__in=references).delete()
        _, created = OriginalRemoval.objects.get_or_create(
            organisation_id=effect.organisation_id,
            sha256=effect.sha256,
            defaults={"removed_at": moment, "reason": effect.reason},
        )
        logger.info(
            "ownership change: organisation %s loses file %s (%s); %s case row(s) reset, "
            "removal mark %s",
            effect.organisation_id,
            effect.sha256[:12],
            effect.reason,
            deleted,
            "created" if created else "already pending",
        )
