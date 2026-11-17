"""The two marks and the audit record of customer data deletion (ADR 0045, issue #85).

Four tables, registered with the project app by `marceline/models.py`:

- `OrganisationEnding` — one organisation whose pilot has ended. `ended_at` is a date a human set;
  nothing derives it from a lapsed subscription, a last login or an empty account. Deletion is the
  one thing in this product that cannot be undone, so it never starts from an inference.
- `OriginalRemoval` — one work of one organisation, keyed by **organisation x sha256** exactly as
  `WorkRights` and `OriginalPublicId` are, because it usually outlives the ownership row that
  created it: the common case is an owner switch, after which the file is somebody else's and the
  previous owner's data has to be findable without it.
- `DataDeletionRecord` — what survives a deletion. It has no foreign key to the organisation on
  purpose, and a trigger refuses UPDATE and DELETE on it (`marceline/0019`): a record of a deletion
  that could itself be deleted answers nothing. It carries ids, digests, dates, counts and codes —
  never a name, a username, a URL or a title.
- `CustomerDataRequest` — the creator's own request for an export or a deletion (ADR 0056,
  issue #133). A request, never an answer: it deletes and exports nothing, and the clock above is
  still a date a human sets.

Only `marceline/deletion/services.py` (marking), `handover.py` (the owner switch),
`requests.py` (the creator's own requests) and `retention.py` (the deletion itself) write these
tables.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class OrganisationEnding(models.Model):
    """One organisation whose pilot ended: the clock for deleting everything it owns."""

    organisation = models.OneToOneField(
        "accounts.Organisation",
        on_delete=models.CASCADE,
        related_name="ending",
        help_text="The customer whose pilot ended. The durable record of the deletion is "
        "DataDeletionRecord, which survives this row.",
    )
    ended_at = models.DateTimeField(
        help_text="When the pilot ended. Set deliberately by staff; never inferred from activity."
    )
    reason = models.CharField(
        max_length=200, blank=True, help_text="Short staff note. No personal data."
    )
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Staff account that marked the pilot ended; empty for the command line.",
    )
    marked_at = models.DateTimeField(default=timezone.now)
    exported_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When `export_customer_data` last wrote this organisation's export. The export "
        "is offered, never required: this row records it, it does not gate the deletion.",
    )
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the deletion completed. Empty while it is pending, and still empty after "
        "a pass that had to keep rows back (an invoice under retention, a legal hold) — the job "
        "retries daily and finishes once the hold is gone.",
    )

    class Meta:
        verbose_name = "organisation ending"
        verbose_name_plural = "organisation endings"
        ordering = ("-ended_at",)

    def __str__(self) -> str:
        return f"organisation {self.organisation_id} ended {self.ended_at:%Y-%m-%d}"


class OriginalRemoval(models.Model):
    """One work of one organisation, marked for removal: the clock for that original alone."""

    class Reason(models.TextChoices):
        OWNER_CHANGED = "owner_changed", "the original moved to another organisation"
        UNASSIGNED = "unassigned", "the original was unassigned (it belongs to nobody now)"
        CUSTOMER_REQUEST = "customer_request", "the customer asked for this work to be removed"
        CLAIM_WITHDRAWN = "claim_withdrawn", "the rights claim on this work was withdrawn or lost"
        STAFF = "staff", "staff decision"

    organisation = models.ForeignKey(
        "accounts.Organisation",
        on_delete=models.CASCADE,
        related_name="original_removals",
        help_text="The organisation losing this work — usually the previous owner.",
    )
    sha256 = models.CharField(
        max_length=64,
        db_index=True,
        help_text="The file, as ADR 0031 identifies an original. Not a Reference foreign key: the "
        "references may already belong to somebody else, or be gone.",
    )
    removed_at = models.DateTimeField(
        help_text="The clock: this organisation's data about this work is deleted this long plus "
        "CUSTOMER_DATA_RETENTION_DAYS later."
    )
    reason = models.CharField(max_length=24, choices=Reason)
    note = models.TextField(blank=True, help_text="Short staff note. No personal data.")
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Staff account behind the mark; empty for the command line and for a move.",
    )
    marked_at = models.DateTimeField(default=timezone.now)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "original removal"
        verbose_name_plural = "original removals"
        ordering = ("-removed_at",)
        constraints = (
            models.UniqueConstraint(
                fields=("organisation", "sha256"), name="original_removal_one_per_file"
            ),
        )

    def __str__(self) -> str:
        return f"{self.sha256[:12]}… of organisation {self.organisation_id} ({self.reason})"


class DataDeletionRecord(models.Model):
    """What a completed deletion leaves behind: who, when, which scope, how much (ADR 0045).

    Append-only at the database level (`marceline/0019`): no UPDATE, no DELETE, ever. It names no
    person and no work — the organisation is its primary key and public id as they were, an
    original is its digest, and everything else is a count, a date or a code.
    """

    class Scope(models.TextChoices):
        ORGANISATION = "organisation"
        ORIGINAL = "original"

    scope = models.CharField(max_length=16, choices=Scope, db_index=True)
    organisation_pk = models.PositiveIntegerField(
        help_text="The organisation's primary key at deletion time. A plain integer, not a foreign "
        "key: this row has to survive the organisation it is about."
    )
    organisation_public_id = models.CharField(
        max_length=48, blank=True, help_text="Its org_… id at deletion time (ADR 0033)."
    )
    sha256 = models.CharField(max_length=64, blank=True, help_text="Original scope only.")
    marked_at = models.DateTimeField(help_text="When the ending or removal was marked.")
    due_at = models.DateTimeField(help_text="Clock plus the window: the earliest this could run.")
    deleted_at = models.DateTimeField(default=timezone.now)
    window_days = models.PositiveIntegerField(help_text="CUSTOMER_DATA_RETENTION_DAYS at the time.")
    counts = models.JSONField(
        default=dict, blank=True, help_text="Rows deleted per table name. Counts only."
    )
    kept = models.JSONField(
        default=list,
        blank=True,
        help_text="Codes of what was deliberately kept: billing_records_retained, legal_hold, "
        "shared_file, staff_account, account_protected, organisation_protected.",
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Staff account that ran it; empty for the scheduler.",
    )

    class Meta:
        verbose_name = "data deletion record"
        verbose_name_plural = "data deletion records"
        ordering = ("-deleted_at",)
        indexes = (models.Index(fields=("organisation_pk", "-deleted_at")),)

    def __str__(self) -> str:
        subject = self.sha256[:12] + "…" if self.sha256 else f"organisation {self.organisation_pk}"
        return f"{self.scope}: {subject} deleted {self.deleted_at:%Y-%m-%d}"


class CustomerDataRequest(models.Model):
    """One creator's own request for an export of their data, or for its deletion (ADR 0056).

    The gap this closes is #133: on the public host the export route is development-only and no
    deletion route existed at all, so an invited creator had no in-product way to ask for an export
    (Art. 15/20 GDPR) or a deletion (Art. 17). This row is the request, not the answer — **nothing here
    deletes or exports anything.** ADR 0045's rule stands unchanged: the clock for a deletion is
    `OrganisationEnding.ended_at`, a date a human sets with `end_pilot`, and the export is produced
    by `export_customer_data`. A request is the statement that the creator asked, when they asked,
    and where staff stand with it — the one thing that was missing, and the one thing a statutory
    one-month deadline (Art. 12(3) GDPR) needs recorded.

    One open request per organisation per kind (`data_request_one_open_per_kind`): a second press of
    the button is not a second request, and the creator can withdraw the open one while it is open.
    A settled request is never edited into a new one — it stays as the record of what was asked.
    """

    class Kind(models.TextChoices):
        EXPORT = "export", "a copy of the organisation's own data (Art. 15, Art. 20)"
        DELETION = "deletion", "deletion of the organisation's data / end of the pilot (Art. 17)"

    class Status(models.TextChoices):
        REQUESTED = "requested", "asked for, nobody has picked it up yet"
        IN_PROGRESS = "in_progress", "staff are working on it"
        DONE = "done", "answered"
        WITHDRAWN = "withdrawn", "the creator took it back before it was answered"

    #: Statuses in which the request is still the organisation's open one.
    OPEN_STATUSES = (Status.REQUESTED, Status.IN_PROGRESS)

    organisation = models.ForeignKey(
        "accounts.Organisation",
        on_delete=models.CASCADE,
        related_name="data_requests",
        help_text="The organisation whose data the request is about. Never another one's.",
    )
    kind = models.CharField(max_length=16, choices=Kind)
    status = models.CharField(max_length=16, choices=Status, default=Status.REQUESTED)
    requested_at = models.DateTimeField(default=timezone.now)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The member who asked; empty once that account is gone.",
    )
    settled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When it was answered or withdrawn. Empty while it is open.",
    )
    staff_note = models.CharField(
        max_length=200,
        blank=True,
        help_text="Short staff note: what was sent, or which end date was set. No personal data, "
        "and never a copy of what was exported.",
    )

    class Meta:
        verbose_name = "customer data request"
        verbose_name_plural = "customer data requests"
        ordering = ("-requested_at", "-pk")
        constraints = (
            models.UniqueConstraint(
                fields=("organisation", "kind"),
                condition=models.Q(status__in=("requested", "in_progress")),
                name="data_request_one_open_per_kind",
            ),
        )

    def __str__(self) -> str:
        return f"{self.kind} request of organisation {self.organisation_id} ({self.status})"

    @property
    def open(self) -> bool:
        return self.status in self.OPEN_STATUSES
