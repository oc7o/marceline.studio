"""Deleting a customer's data once its window is up (ADR 0045, issue #85).

The house pattern of `index/retention.py` (ADR 0029) and `billing/retention.py` (ADR 0032): a dry
run by default, counts only, protected rows reported as kept rather than failing the run,
idempotent, scheduled last and off until `CUSTOMER_DATA_DELETION_ENABLED`.

Two scopes, one clock each:

- **organisation** — `OrganisationEnding.ended_at` plus `CUSTOMER_DATA_RETENTION_DAYS`: everything
  that organisation registered and everything we recorded for it;
- **original** — `OriginalRemoval.removed_at` plus the same window, for one organisation and one
  file: what an owner switch, a withdrawn claim or a customer removing one work leaves behind.

**What is never deleted here**, whatever the marks say:

- pages, page media, page frame hashes, clusters, common frames and breadcrumbs. They are ours: we
  crawled them, and a customer leaving does not unmake what the index knows about a pirate site.
  Their retention is `page_retention` (ADR 0029);
- a file another organisation owns a reference of — the reference set is filtered by ownership at
  deletion time, so an original that moved on is untouched, and unassigned twins belong to nobody;
- billing (ADR 0032). Invoices, cancellations, subscriptions and the billing profile all `PROTECT`
  the organisation, so the organisation row simply survives its own pass while any of them stands,
  and the pass says so. `invoice_retention` is the only thing that ever deletes an invoice, and the
  billing trigger refuses it before its eight years are up;
- anything of the organisation while **any** of its rights rows is on `legal_hold` (ADR 0034): the
  whole rights block is kept together, because a held record's mandate, schedule, proofs and event
  log are the record. A trigger (`marceline/0019`) refuses deleting a held row even by raw SQL;
- safety incidents, their pages and the hashes copied onto them (ADR 0011/0018). Nothing here
  reads or writes them: they are a record about pages we had to report, not about a customer;
- staff accounts. A `is_staff`/`is_superuser` user holding a membership loses the membership and
  keeps the account.

Production (ADR 0028) is told twice, and both mechanisms already existed: a `delete` cache purge
naming the organisation is written in the same transaction and delivered on commit, and the next
`pull_accounts` deletes the users and the organisation there because the accounts document no
longer exports them.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from accounts.models import Membership, Organisation
from billing.models import BillingProfile, Invoice, Subscription
from core import deletion_journal
from delivery.models import PurgeOutbox
from marceline.app.ownership import owned_references
from marceline.cache_purge import request_purge
from marceline.deletion.models import (
    CustomerDataRequest,
    DataDeletionRecord,
    OrganisationEnding,
    OriginalRemoval,
)
from marceline.deletion.services import due_at, window
from marceline.models import OriginalPublicId, RefusedClaim, RightsConfirmation, UploadAttempt
from marceline.notices.models import NoticeConfirmation, NoticeDraft, NoticeEvent, NoticeFinding
from marceline.rights import vault
from marceline.rights.models import (
    AccountControlChallenge,
    AgencyMandate,
    LicensedPartner,
    ProofDocument,
    RightsClaimant,
    RightsEvent,
    RightsSchedule,
    WorkRights,
    WorkRightsTransition,
)
from matching.models import CaseFinding, LeakCase, Reference, ReferenceFrameHash, ReferenceMatch

logger = logging.getLogger("marceline.deletion")

#: Codes for what a pass deliberately kept. They go into `DataDeletionRecord.kept`.
KEPT_BILLING = "billing_records_retained"
KEPT_LEGAL_HOLD = "legal_hold"
KEPT_SHARED_FILE = "shared_file"
KEPT_STAFF_ACCOUNT = "staff_account"
KEPT_ACCOUNT_PROTECTED = "account_protected"
KEPT_ORGANISATION = "organisation_protected"
#: The kept codes that also keep the `Organisation` row itself alive (all three are `PROTECT`
#: relations in the database, which is what makes this a prediction and not a guess).
PROTECTING_KEPT = (KEPT_BILLING, KEPT_LEGAL_HOLD, KEPT_SHARED_FILE)


@dataclass
class ScopeReport:
    """One organisation or one original, as this pass found it. Counts, codes and ids only."""

    scope: str
    organisation_pk: int
    organisation_public_id: str
    #: For the printed report only — never stored in `DataDeletionRecord`.
    organisation_name: str
    sha256: str
    marked_at: datetime
    due_at: datetime
    counts: dict[str, int] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)
    #: False when something had to stay behind (billing, a legal hold): the mark stays pending and
    #: the job tries again, so a lifted hold finishes the job without anyone remembering it.
    complete: bool = True

    @property
    def rows(self) -> int:
        return sum(self.counts.values())


@dataclass
class RetentionReport:
    now: datetime
    dry_run: bool
    window_days: int
    #: Marks that exist at all, whatever their age.
    pending_organisations: int
    pending_originals: int
    organisations: list[ScopeReport] = field(default_factory=list)
    originals: list[ScopeReport] = field(default_factory=list)

    @property
    def due(self) -> int:
        return len(self.organisations) + len(self.originals)


def _count(counts: dict[str, int], label: str, number: int) -> None:
    if number:
        counts[label] = counts.get(label, 0) + number


def _shared_files(organisation: Organisation) -> set[str]:
    """Digests this organisation owns a reference of that another organisation owns too.

    ADR 0031 moves every reference of a file together, so this is empty in a healthy database. It
    is checked anyway: "another organisation's original is never deleted" is a guarantee, and a
    guarantee that depends on another module's invariant is a hope.
    """
    digests = set(owned_references(organisation).values_list("sha256", flat=True))
    if not digests:
        return set()
    return set(
        Reference.objects.filter(sha256__in=digests, ownership__isnull=False)
        .exclude(ownership__organisation=organisation)
        .values_list("sha256", flat=True)
    )


def _rights_held(organisation: Organisation, sha256: str = "") -> bool:
    """Is any rights row of this organisation (optionally: of this file) on legal hold?

    The whole rights block is kept together when one row is held: a held work's mandate, schedule,
    proofs and event log are part of the record, and picking it apart is how a hold gets lost.
    """
    works = WorkRights.objects.filter(organisation=organisation)
    proofs = ProofDocument.objects.filter(organisation=organisation)
    if sha256:
        works = works.filter(sha256=sha256)
        proofs = proofs.filter(work__sha256=sha256)
        return works.filter(legal_hold=True).exists() or proofs.filter(legal_hold=True).exists()
    return (
        works.filter(legal_hold=True).exists()
        or proofs.filter(legal_hold=True).exists()
        or AgencyMandate.objects.filter(organisation=organisation, legal_hold=True).exists()
        or RightsSchedule.objects.filter(organisation=organisation, legal_hold=True).exists()
    )


def _delete_proofs(proofs: Sequence[ProofDocument], counts: dict[str, int], dry_run: bool) -> None:
    """Vault file first, then the row — the order `rights_housekeeping` uses."""
    _count(counts, "rights_proofs", len(proofs))
    if dry_run:
        return
    for proof in proofs:
        vault.delete(proof.storage_name)
        proof.delete()  # its access log goes with it (RightsEvent.proof CASCADE)


def _delete_notices(drafts: Q, counts: dict[str, int], dry_run: bool) -> None:
    rows = NoticeDraft.objects.filter(drafts)
    _count(counts, "notice_drafts", rows.count())
    _count(counts, "notice_findings", NoticeFinding.objects.filter(draft__in=rows).count())
    _count(
        counts, "notice_confirmations", NoticeConfirmation.objects.filter(draft__in=rows).count()
    )
    _count(counts, "notice_events", NoticeEvent.objects.filter(draft__in=rows).count())
    if not dry_run:
        rows.delete()


def _delete_references(references: Sequence[int], counts: dict[str, int], dry_run: bool) -> None:
    """Delete these originals: frame hashes, matches, the case file and ownership go with them."""
    rows = Reference.objects.filter(pk__in=references)
    cases = LeakCase.objects.filter(reference__in=rows)
    _count(counts, "originals", rows.count())
    _count(
        counts, "reference_frames", ReferenceFrameHash.objects.filter(reference__in=rows).count()
    )
    _count(counts, "matches", ReferenceMatch.objects.filter(reference__in=rows).count())
    _count(counts, "cases", cases.count())
    _count(counts, "case_findings", CaseFinding.objects.filter(case__in=cases).count())
    _count(
        counts,
        "rights_confirmations",
        RightsConfirmation.objects.filter(reference__in=rows).count(),
    )
    if not dry_run:
        rows.delete()


def _delete_rights(organisation: Organisation, counts: dict[str, int], dry_run: bool) -> None:
    """Every rights row of this organisation. Only called when nothing of it is on legal hold."""
    works = WorkRights.objects.filter(organisation=organisation)
    _count(counts, "work_rights", works.count())
    _count(
        counts,
        "rights_transitions",
        WorkRightsTransition.objects.filter(work__in=works).count(),
    )
    _delete_proofs(list(ProofDocument.objects.filter(organisation=organisation)), counts, dry_run)
    _count(counts, "rights_events", RightsEvent.objects.filter(organisation=organisation).count())
    mandates = AgencyMandate.objects.filter(organisation=organisation)
    schedules = RightsSchedule.objects.filter(organisation=organisation)
    partners = LicensedPartner.objects.filter(organisation=organisation)
    claimants = RightsClaimant.objects.filter(organisation=organisation)
    # ADR 0055: the platform accounts this organisation claimed. Named here rather than left to the
    # organisation row's CASCADE, because one legal hold keeps that row — and then the claimed
    # handles would be the only thing of the rights block still standing.
    challenges = AccountControlChallenge.objects.filter(organisation=organisation)
    _count(counts, "account_challenges", challenges.count())
    _count(counts, "agency_mandates", mandates.count())
    _count(counts, "rights_schedules", schedules.count())
    _count(counts, "licensed_partners", partners.count())
    _count(counts, "rights_claimants", claimants.count())
    if dry_run:
        return
    works.delete()  # transitions CASCADE
    RightsEvent.objects.filter(organisation=organisation).delete()
    mandates.delete()
    schedules.delete()
    partners.delete()
    challenges.delete()  # their append-only event log CASCADEs with them
    claimants.delete()


def _delete_accounts(
    organisation: Organisation, counts: dict[str, int], kept: list[str], dry_run: bool
) -> None:
    """Memberships and the customer accounts behind them; staff accounts keep their login."""
    memberships = Membership.objects.filter(organisation=organisation)
    users = User.objects.filter(memberships__organisation=organisation).distinct()
    customers = users.filter(is_staff=False, is_superuser=False)
    if users.exclude(pk__in=customers.values("pk")).exists():
        kept.append(KEPT_STAFF_ACCOUNT)
    _count(counts, "memberships", memberships.count())
    _count(counts, "users", customers.count())
    if dry_run:
        return
    customer_ids = list(customers.values_list("pk", flat=True))
    memberships.delete()
    for user_id in customer_ids:
        # One by one, because a row we deliberately kept (a rights confirmation on a shared file)
        # `PROTECT`s its author: that account stays and the rest still go.
        try:
            with transaction.atomic():
                User.objects.filter(pk=user_id).delete()
        except ProtectedError:
            counts["users"] = max(0, counts.get("users", 0) - 1)
            if KEPT_STAFF_ACCOUNT not in kept:
                kept.append(KEPT_ACCOUNT_PROTECTED)


def _billing_rows(organisation: Organisation) -> int:
    return (
        Invoice.objects.filter(organisation=organisation).count()
        + Subscription.objects.filter(organisation=organisation).count()
        + BillingProfile.objects.filter(organisation=organisation).count()
    )


def _purge(organisation_pk: int, *, dry_run: bool) -> list[PurgeOutbox]:
    """Tell production to drop this organisation's cached bundles (ADR 0028).

    Written inside the deletion's own transaction, delivered on commit — the same path a safety
    flag uses. `pull_accounts` then removes the login on production within 15 minutes because the
    accounts document stops exporting it.
    """
    if dry_run:
        return []
    return request_purge(
        PurgeOutbox.Mode.DELETE, reason="customer_deletion", organisation_ids=[organisation_pk]
    )


def _record(report: ScopeReport, *, by: User | None, dry_run: bool) -> None:
    if dry_run or not report.counts:
        return
    DataDeletionRecord.objects.create(
        scope=report.scope,
        organisation_pk=report.organisation_pk,
        organisation_public_id=report.organisation_public_id,
        sha256=report.sha256,
        marked_at=report.marked_at,
        due_at=report.due_at,
        window_days=int(settings.CUSTOMER_DATA_RETENTION_DAYS),
        counts=dict(report.counts),
        kept=sorted(set(report.kept)),
        performed_by=by,
    )


def delete_organisation(
    ending: OrganisationEnding,
    *,
    now: datetime,
    dry_run: bool = True,
    by: User | None = None,
) -> ScopeReport:
    """Delete everything this organisation's pilot left behind. Idempotent."""
    organisation = ending.organisation
    report = ScopeReport(
        scope=DataDeletionRecord.Scope.ORGANISATION,
        organisation_pk=organisation.pk,
        organisation_public_id=organisation.public_id,
        organisation_name=organisation.name,
        sha256="",
        marked_at=ending.ended_at,
        due_at=due_at(ending.ended_at),
    )
    with transaction.atomic():
        shared = _shared_files(organisation)
        if shared:
            report.kept.append(KEPT_SHARED_FILE)
        references = list(
            owned_references(organisation)
            .exclude(sha256__in=shared)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        _delete_notices(Q(organisation=organisation), report.counts, dry_run)
        if _rights_held(organisation):
            report.kept.append(KEPT_LEGAL_HOLD)
        else:
            _delete_rights(organisation, report.counts, dry_run)
        _count(
            report.counts,
            "refused_claims",
            RefusedClaim.objects.filter(organisation=organisation).count(),
        )
        _count(
            report.counts,
            "upload_attempts",
            UploadAttempt.objects.filter(organisation=organisation).count(),
        )
        _count(
            report.counts,
            "original_public_ids",
            OriginalPublicId.objects.filter(organisation=organisation).count(),
        )
        # ADR 0056: the organisation's own data requests. Counted and deleted here rather than left
        # to the CASCADE, because a legal hold can keep the organisation row and a list of "delete
        # my data" requests is the last thing that should outlive the deletion it asked for. The
        # `DataDeletionRecord` is what remains, and it names no person.
        _count(
            report.counts,
            "data_requests",
            CustomerDataRequest.objects.filter(organisation=organisation).count(),
        )
        if not dry_run:
            RefusedClaim.objects.filter(organisation=organisation).delete()
            UploadAttempt.objects.filter(organisation=organisation).delete()
            OriginalPublicId.objects.filter(organisation=organisation).delete()
            CustomerDataRequest.objects.filter(organisation=organisation).delete()
        _delete_references(references, report.counts, dry_run)
        _delete_accounts(organisation, report.counts, report.kept, dry_run)
        if _billing_rows(organisation):
            report.kept.append(KEPT_BILLING)
        report.complete = _finish_organisation(organisation, report, dry_run=dry_run)
        _purge(organisation.pk, dry_run=dry_run)
        _record(report, by=by, dry_run=dry_run)
        # ADR 0057: `DataDeletionRecord` above is in the database, so a restore loses the record of
        # the deletion together with the deletion itself. The journal is outside the dump and holds
        # the organisation id only — no name, no public id, no sha256 of a customer's file.
        if not dry_run and report.counts:
            deletion_journal.record(
                "organisation_data_deleted", organisation=organisation.pk, now=now
            )
        if not dry_run and report.complete:
            OrganisationEnding.objects.filter(pk=ending.pk).update(deleted_at=now)
    if not dry_run:
        logger.warning(
            "CUSTOMER DATA DELETION organisation %s: %s row(s) deleted, kept %s",
            organisation.pk,
            report.rows,
            ", ".join(sorted(set(report.kept))) or "nothing",
        )
    return report


def _finish_organisation(organisation: Organisation, report: ScopeReport, *, dry_run: bool) -> bool:
    """Delete the organisation row itself, unless something protects it. True when it is gone."""
    if dry_run:
        protected = any(code in report.kept for code in PROTECTING_KEPT)
        if protected:
            report.kept.append(KEPT_ORGANISATION)
        else:
            _count(report.counts, "organisations", 1)
        return not protected
    try:
        with transaction.atomic():
            Organisation.objects.filter(pk=organisation.pk).delete()
    except ProtectedError:
        report.kept.append(KEPT_ORGANISATION)
        return False
    _count(report.counts, "organisations", 1)
    return True


def delete_original(
    removal: OriginalRemoval,
    *,
    now: datetime,
    dry_run: bool = True,
    by: User | None = None,
) -> ScopeReport:
    """Delete one organisation's data about one file. Idempotent.

    The references of that file are deleted **only while this organisation still owns them**: after
    an owner switch they are the new owner's originals and this pass must not touch them.
    """
    organisation = removal.organisation
    report = ScopeReport(
        scope=DataDeletionRecord.Scope.ORIGINAL,
        organisation_pk=organisation.pk,
        organisation_public_id=organisation.public_id,
        organisation_name=organisation.name,
        sha256=removal.sha256,
        marked_at=removal.removed_at,
        due_at=due_at(removal.removed_at),
    )
    with transaction.atomic():
        references = list(
            owned_references(organisation)
            .filter(sha256=removal.sha256)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if not references and Reference.objects.filter(sha256=removal.sha256).exists():
            report.kept.append(KEPT_SHARED_FILE)
        _delete_notices(Q(organisation=organisation, sha256=removal.sha256), report.counts, dry_run)
        if _rights_held(organisation, removal.sha256):
            report.kept.append(KEPT_LEGAL_HOLD)
            report.complete = False
        else:
            works = WorkRights.objects.filter(organisation=organisation, sha256=removal.sha256)
            _count(report.counts, "work_rights", works.count())
            _count(
                report.counts,
                "rights_transitions",
                WorkRightsTransition.objects.filter(work__in=works).count(),
            )
            _delete_proofs(
                list(ProofDocument.objects.filter(work__in=works)), report.counts, dry_run
            )
            if not dry_run:
                works.delete()
        claims = RefusedClaim.objects.filter(organisation=organisation, sha256=removal.sha256)
        public_ids = OriginalPublicId.objects.filter(
            organisation=organisation, sha256=removal.sha256
        )
        confirmations = RightsConfirmation.objects.filter(
            organisation=organisation, reference__sha256=removal.sha256
        )
        _count(report.counts, "refused_claims", claims.count())
        _count(report.counts, "original_public_ids", public_ids.count())
        if not references:
            # Otherwise they go with the references below, and would be counted twice.
            _count(report.counts, "rights_confirmations", confirmations.count())
        if not dry_run:
            claims.delete()
            public_ids.delete()
            if not references:
                confirmations.delete()
        _delete_references(references, report.counts, dry_run)
        _purge(organisation.pk, dry_run=dry_run)
        _record(report, by=by, dry_run=dry_run)
        # ADR 0057, as above. The file's sha256 is the scope's own key (ADR 0045) and names nobody.
        if not dry_run and report.counts:
            deletion_journal.record(
                "original_data_deleted",
                organisation=organisation.pk,
                sha256=removal.sha256,
                now=now,
            )
        if not dry_run and report.complete:
            OriginalRemoval.objects.filter(pk=removal.pk).update(deleted_at=now)
    if not dry_run:
        logger.warning(
            "CUSTOMER DATA DELETION organisation %s file %s: %s row(s) deleted, kept %s",
            organisation.pk,
            removal.sha256[:12],
            report.rows,
            ", ".join(sorted(set(report.kept))) or "nothing",
        )
    return report


def apply_retention(
    *,
    now: datetime | None = None,
    dry_run: bool = True,
    by: User | None = None,
) -> RetentionReport:
    """Delete everything whose window is up, both scopes. Idempotent; `dry_run` only reports."""
    moment = now or timezone.now()
    cutoff = moment - window()
    report = RetentionReport(
        now=moment,
        dry_run=dry_run,
        window_days=int(settings.CUSTOMER_DATA_RETENTION_DAYS),
        pending_organisations=OrganisationEnding.objects.filter(deleted_at__isnull=True).count(),
        pending_originals=OriginalRemoval.objects.filter(deleted_at__isnull=True).count(),
    )
    endings = (
        OrganisationEnding.objects.select_related("organisation")
        .filter(deleted_at__isnull=True, ended_at__lte=cutoff)
        .order_by("pk")
    )
    for ending in list(endings):
        report.organisations.append(delete_organisation(ending, now=moment, dry_run=dry_run, by=by))
    removals = (
        OriginalRemoval.objects.select_related("organisation")
        .filter(deleted_at__isnull=True, removed_at__lte=cutoff)
        .order_by("pk")
    )
    for removal in list(removals):
        report.originals.append(delete_original(removal, now=moment, dry_run=dry_run, by=by))
    return report
