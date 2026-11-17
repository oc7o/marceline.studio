"""Who owns an original: one row per assigned `matching.Reference` (ADR 0031, issue #76).

The column lives here, in the project app, and not on `Reference`: an organisation is an
`accounts` concept and import-linter forbids `matching -> accounts`. A string foreign key on
`Reference` would slip past the linter (it is not a Python import) while making `matching`'s
schema and migrations depend on `accounts` — exactly the silent loosening the contracts exist to
refuse. The project package may depend on every app, so the join table sits where the dependency
is already allowed.

Only `marceline/app/ownership.py` reads or writes these tables.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.public_ids import public_id_field


class OriginalOwnership(models.Model):
    """The organisation that owns one original. No row means unassigned: visible to no customer.

    Set once when the original is registered, and changed afterwards only deliberately (admin with
    confirmation, or `manage.py assign_originals`). Membership changes never touch it: a member who
    leaves, moves or is deleted takes nothing with them. `Reference.uploaded_by` stays the audit of
    who registered the file; it decides nothing.
    """

    class Via(models.TextChoices):
        UPLOAD = "upload", "staff upload API"
        CUSTOMER = "customer", "customer added it in the app (issue #29)"
        IMPORT = "import", "CLI import with --organisation"
        INHERITED = "inherited", "inherited from an earlier reference of the same file"
        BACKFILL = "backfill", "backfilled from the uploader's membership (issue #76)"
        COMMAND = "command", "assign_originals"
        ADMIN = "admin", "admin"

    reference = models.OneToOneField(
        "matching.Reference",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="ownership",
    )
    organisation = models.ForeignKey(
        "accounts.Organisation",
        on_delete=models.PROTECT,
        related_name="owned_originals",
        help_text="An organisation that owns originals cannot be deleted; reassign them first.",
    )
    assigned_via = models.CharField(max_length=16, choices=Via)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Staff account behind the last assignment; empty for the CLI and the backfill.",
    )
    assigned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "original ownership"
        verbose_name_plural = "original ownerships"
        indexes = (models.Index(fields=("organisation", "reference")),)

    def __str__(self) -> str:
        return f"original {self.reference_id} -> organisation {self.organisation_id}"


class RightsConfirmation(models.Model):
    """What a customer declared when adding an original in the app (issue #29).

    Before a browser upload is accepted the customer ticks "I own, or am authorised to enforce, the
    rights in this video". This row is that declaration: who, for which organisation, when, and
    which wording in which language (`marceline/app/rights_confirmation.json` holds every version's
    text, so the record never depends on today's copy). It is a claim, not proof of rights — proof
    and authorisation documents are a separate, lawyer-reviewed step (docs/legal/checklist.md 3.2).

    Only customer uploads write it; staff and CLI imports carry none. The author is protected: an
    account with a declaration on file cannot be deleted by accident.
    """

    reference = models.OneToOneField(
        "matching.Reference",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="rights_confirmation",
    )
    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT, related_name="+"
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    text_version = models.CharField(
        max_length=32, help_text="Key into marceline/app/rights_confirmation.json."
    )
    language = models.CharField(max_length=8, help_text="Language the text was shown in.")
    confirmed_at = models.DateTimeField(default=timezone.now)
    work_rights = models.ForeignKey(
        "marceline.WorkRights",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="upload_confirmations",
        help_text="The work's rights record this tick self-declared (ADR 0034). This row stays the "
        "upload's audit record; the rights record carries the status and its log.",
    )

    class Meta:
        verbose_name = "rights confirmation"
        verbose_name_plural = "rights confirmations"

    def __str__(self) -> str:
        return (
            f"original {self.reference_id} confirmed by {self.confirmed_by_id} "
            f"({self.text_version})"
        )


class UploadAttempt(models.Model):
    """One admitted customer write to the fingerprint or rights API, counted per organisation.

    The per-organisation rate limit (`marceline/app/upload_throttle.py`) counts these rows in a
    rolling hour; they carry no content, and rows older than the window are deleted when the next
    attempt of that organisation is admitted.
    """

    class Kind(models.TextChoices):
        ORIGINAL = "original"
        CLIENT_EVENT = "client_event"
        RIGHTS_WRITE = "rights_write"
        NOTICE_WRITE = "notice_write"
        EXPORT = "export"

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.CASCADE, related_name="+"
    )
    kind = models.CharField(max_length=16, choices=Kind)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = (models.Index(fields=("organisation", "kind", "created_at")),)

    def __str__(self) -> str:
        return f"{self.kind} by organisation {self.organisation_id} at {self.created_at:%H:%M:%S}"


class OriginalPublicId(models.Model):
    """The id a customer knows one original by: one file, as one organisation owns it (ADR 0033).

    A customer original is every reference of one file (sha256) that the organisation owns (ADR
    0031), so its public id cannot live on a `Reference` — the representative changes when a newer
    re-fingerprint arrives, and an old one may be retired. Keyed by (organisation, sha256) instead,
    the id survives both. A file moved to another organisation gets a fresh id there (the old row
    stays, answering 404 like any unknown id), and gets its old id back if it ever returns.

    Written by `ownership.register` / `ownership.reassign` when an owner is set, and filled in by
    the customer reads for any owned file that has none yet.
    """

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.CASCADE, related_name="+"
    )
    sha256 = models.CharField(max_length=64)
    public_id = public_id_field("orig")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "original public id"
        verbose_name_plural = "original public ids"
        constraints = (
            models.UniqueConstraint(
                fields=("organisation", "sha256"), name="original_public_id_one_per_file"
            ),
        )

    def __str__(self) -> str:
        return f"{self.public_id} (organisation {self.organisation_id})"


class RefusedClaim(models.Model):
    """A customer upload refused with the generic `claim_needs_review` 409 (operator 2026-09-17).

    `marceline/app/fingerprints.py` answers every refused claim with one neutral 409 that names
    nobody — the customer never learns which case applied. This row is where that reason, the
    organisation and the original(s) involved actually go, for `marceline/review/claims.py`'s
    staff-only queue. It lives here, not in `matching`, for the same reason `OriginalOwnership`
    does: it joins an `accounts.Organisation` to `matching.Reference` ids, and only the project
    package may depend on both.
    """

    class Reason(models.TextChoices):
        OWNED_BY_OTHER = "owned_by_other", "Same file, owned by another organisation"
        UNOWNED_EXISTING = "unowned_existing", "Same file, registered but owned by nobody"
        HASH_OVERLAP = "hash_overlap", "Hashes largely duplicate another organisation's original"

    class Status(models.TextChoices):
        OPEN = "open"
        RESOLVED = "resolved"
        DISMISSED = "dismissed"

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.CASCADE, related_name="refused_claims"
    )
    sha256 = models.CharField(max_length=64)
    reason = models.CharField(max_length=32, choices=Reason)
    overlap_share = models.FloatField(
        null=True,
        blank=True,
        help_text="Share of this upload's hashes already stored elsewhere; only for hash_overlap.",
    )
    reference_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="matching.Reference ids of the original(s) this claim touched. Plain ids, not "
        "a foreign key: this is staff-only evidence, never a live relation a deletion must fix up.",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Customer account behind the refused upload.",
    )
    status = models.CharField(max_length=16, choices=Status, default=Status.OPEN, db_index=True)
    note = models.TextField(blank=True, help_text="Staff note when resolving or dismissing.")
    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "refused claim"
        verbose_name_plural = "refused claims"
        ordering = ("-created_at",)
        indexes = (models.Index(fields=("status", "-created_at")),)

    def __str__(self) -> str:
        return f"{self.organisation_id}: {self.sha256[:12]} ({self.reason})"


class CreatorRetentionClock(models.Model):
    """When a catalogue name was first seen to be used by nothing (ADR 0048, issue #104).

    The retention clock for `catalog.Creator`, and the same shape as `index.Page.deactivated_at`:
    `marceline.name_retention` sets it the first time none of its keep rules holds for that name,
    clears it the moment one holds again, and `CREATOR_NAME_RETENTION_DAYS` after it was set the
    name is deleted with its aliases.

    It lives here rather than as a column on `catalog.Creator` because deciding "unused" needs
    `matching`, `index`, `accounts` and `marceline.targets` together, which `catalog` may not
    import. `CASCADE`, so the clock disappears with the row it was counting.

    **No name, ever.** The row is a foreign key and a timestamp; a retention record that copied the
    name would defeat the deletion it records.
    """

    creator = models.OneToOneField(
        "catalog.Creator", on_delete=models.CASCADE, related_name="retention_clock"
    )
    unused_since = models.DateTimeField(
        help_text="When the retention pass first saw nothing using this name."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "creator retention clock"
        ordering = ("unused_since", "creator_id")

    def __str__(self) -> str:
        return f"creator {self.creator_id} unused since {self.unused_since:%Y-%m-%d}"


class CreatorMergeLog(models.Model):
    """Audit trail of a staff merge or alias of a `catalog.Creator.needs_review` row (operator
    2026-09-17, `marceline/review/creators.py`).

    The source creator row is deleted once its reference cast links, page links and aliases have
    moved to the target, so its name and id are copied here — this is the only place they survive
    the merge. It lives in the project package because the move touches `catalog.Creator`,
    `index.Page` and `matching.Reference` together, which no single app may import.
    """

    source_creator_id = models.PositiveIntegerField()
    source_name = models.CharField(max_length=255)
    target = models.ForeignKey(
        "catalog.Creator", on_delete=models.CASCADE, related_name="merge_log_entries"
    )
    as_alias = models.BooleanField(
        default=False, help_text="True: the source's name was added as an alias of the target."
    )
    moved_references = models.PositiveIntegerField(default=0)
    moved_pages = models.PositiveIntegerField(default=0)
    aliases_added = models.JSONField(default=list, blank=True)
    merged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "creator merge log"
        verbose_name_plural = "creator merge log"
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.source_name} (#{self.source_creator_id}) -> {self.target_id}"


class MediaUpload(models.Model):
    """One customer upload waiting to be fingerprinted, and what became of it (issue #59, ADR 0044).

    **This row never holds the media, a path to it or anything read out of it.** The bytes live in
    the spool (`marceline/uploads/spool.py`), named after `public_id`, and are deleted the moment
    hashing ends — on success, on refusal, on a crash and on a timeout — with `bytes_deleted_at`
    written only after the directory is verified gone. The customer's **file name is never
    transmitted and never stored**: the browser uses it to pre-fill `title`, which is the
    customer's own words and the same field every other import path carries.

    What is kept is what an audit of "did you really delete it" needs — sizes, digests, timestamps
    and codes — plus the metadata the registration needs, which is exactly the metadata a locally
    computed fingerprint document carries anyway.
    """

    class Status(models.TextChoices):
        RECEIVING = "receiving", "receiving chunks"
        QUEUED = "queued", "complete, waiting for the worker"
        HASHING = "hashing", "being fingerprinted"
        DONE = "done", "added as an original"
        REFUSED = "refused", "refused with a code"
        FAILED = "failed", "could not be processed"
        EXPIRED = "expired", "not completed in time; the bytes were reaped"
        CANCELLED = "cancelled", "the customer cancelled it"

    #: Statuses whose bytes may still exist. Everything else has been through `finally`.
    OPEN_STATUSES = (Status.RECEIVING, Status.QUEUED, Status.HASHING)
    #: Statuses a customer may still act on.
    SETTLED_STATUSES = (
        Status.DONE,
        Status.REFUSED,
        Status.FAILED,
        Status.EXPIRED,
        Status.CANCELLED,
    )

    class Origin(models.TextChoices):
        DIRECT = "direct", "sent straight to this machine"
        RELAY = "relay", "forwarded by the public host's streaming relay"

    public_id = public_id_field("upl")
    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.CASCADE, related_name="+"
    )
    #: How the bytes reached this machine (ADR 0044 §9, amendment 2026-09-19). The relay credential
    #: may only append to, complete or cancel a row it created itself — `relay` is what makes that
    #: enforceable rather than a sentence in an ADR.
    origin = models.CharField(max_length=8, choices=Origin, default=Origin.DIRECT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    status = models.CharField(max_length=16, choices=Status, default=Status.RECEIVING)

    #: What the customer declared when starting the upload.
    declared_bytes = models.BigIntegerField()
    declared_sha256 = models.CharField(max_length=64, blank=True)
    title = models.CharField(max_length=255)
    #: The cast the customer typed, primary (rights holder) first — the same list a fingerprint
    #: document carries; the creators are resolved at registration, never here.
    creators = models.JSONField(default=list)
    frame_fps = models.FloatField()
    chunk_bytes = models.PositiveIntegerField()
    #: The rights declaration the customer ticked before the first byte was sent (ADR 0034). The
    #: version is checked again at registration against today's current one, exactly as a browser
    #: upload's is, so a declaration retired in the meantime refuses the job rather than the row.
    rights_text_version = models.CharField(max_length=32)
    rights_language = models.CharField(max_length=2)
    #: The upload declaration (ADR 0044 amendment 2026-09-19,
    #: `marceline/app/upload_declaration.py`): the uploader is the only person in the file,
    #: and consents explicitly under Art. 9(2)(a) GDPR
    #: to the hashing of it. Stored **verbatim** beside its version, its language and the moment it
    #: was made, because a record of consent that cannot be read back is not a record. Empty on rows
    #: created before the declaration existed; a new upload cannot be created without it.
    upload_declaration_version = models.CharField(max_length=48, blank=True)
    upload_declaration_language = models.CharField(max_length=2, blank=True)
    upload_declaration_text = models.TextField(blank=True)
    upload_declaration_at = models.DateTimeField(null=True, blank=True)

    #: What really arrived, and what it hashed to. `sha256` is the spool's own digest, computed
    #: while the chunks were appended, and is what the declared one is compared against.
    received_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    duration_s = models.FloatField(null=True, blank=True)
    #: How far the fingerprinting has come (ADR 0044 amendment 2026-09-18). Two counters written by
    #: the *parent* worker from the numbers its contained child reports over the pipe, throttled
    #: (`uploads/service.py` `ProgressWriter`) so a long video is a handful of writes and not one
    #: per frame. `frames_total` stays NULL until the probe has read a duration — a null total is
    #: "not known yet", which the screen shows as an indeterminate bar rather than inventing a
    #: denominator.
    frames_done = models.PositiveIntegerField(default=0)
    frames_total = models.PositiveIntegerField(null=True, blank=True)
    #: The recipe that ran, so a later job kind (a crop grid, ADR 0039) is distinguishable.
    pipeline = models.CharField(max_length=64, blank=True)

    #: The outcome, as a code. Never a sentence, never anything built from the file.
    result_code = models.CharField(max_length=40, blank=True)
    #: The original this became, as the customer knows it (`orig_…`), or empty.
    original_id = models.CharField(max_length=48, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    hashed_at = models.DateTimeField(null=True, blank=True)
    #: Written **only** after the job's directory is gone and a follow-up check says so.
    bytes_deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "media upload"
        indexes = (
            models.Index(fields=("organisation", "-created_at")),
            models.Index(fields=("status", "created_at")),
        )
        constraints = (
            models.CheckConstraint(
                condition=models.Q(received_bytes__gte=0) & models.Q(declared_bytes__gt=0),
                name="media_upload_sizes_are_sane",
            ),
        )

    def __str__(self) -> str:
        return f"{self.public_id} ({self.status})"


# The rights model (ADR 0034) lives in its own module; importing it here registers its tables with
# this app. Only `marceline.rights.services` writes them.
# Notice drafts (issue #33, ADR 0035) live in their own module for the same reason; importing it
# here registers its tables with this app. Only `marceline.notices.services` writes them.
# Customer data deletion (issue #85, ADR 0045) does the same: the two marks and the audit record
# that survives a deletion. Only `marceline.deletion.{services,handover,retention}` write them.
from marceline.deletion.models import (  # noqa: E402, F401
    DataDeletionRecord,
    OrganisationEnding,
    OriginalRemoval,
)
from marceline.notices.models import (  # noqa: E402, F401
    NoticeConfirmation,
    NoticeDraft,
    NoticeEvent,
    NoticeFinding,
)
from marceline.rights.models import (  # noqa: E402, F401
    AgencyMandate,
    LicensedPartner,
    ProofDocument,
    RightsClaimant,
    RightsEvent,
    RightsSchedule,
    WorkRights,
    WorkRightsTransition,
)

# The upload relay's refused attestations (ADR 0044 §9, amendment 2026-09-20) do the same: a
# counter per hour and reason, written only by `marceline.uploads.attestation.record`.
from marceline.uploads.models import RelayRefusal  # noqa: E402, F401
