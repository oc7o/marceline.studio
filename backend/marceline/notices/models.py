"""Notice draft tables (issue #33, ADR 0035). Only `marceline.notices.services` writes them.

A `NoticeDraft` is canonical per (organisation, recipient provider, channel): it groups one or more
`NoticeFinding` rows (page public ids, as the answer's schema calls them `reported_material[]`).
Everything a customer must personally confirm before a draft may be approved is a separate
`NoticeConfirmation`, stored **verbatim with its version** — what was confirmed is what is shown,
never today's copy of the text. `NoticeEvent` is the append-only audit log (migration
`marceline/0011_notices_append_only`, same technique as the rights logs, ADR 0034): who did what,
when, and — on `approved` — the exact rendered text's hash.

Sandbox (operator decision 2026-09-17): a draft snapshots `organisation.is_sandbox` at creation
(`is_sandbox`), so an operator toggling the flag later never rewrites history. A sandbox draft can
only ever reach `marked_sent_by_customer` through `services.sandbox_send` (an email copy to a
`NOTICE_SANDBOX_RECIPIENTS` address, `notices.sandbox`); it can never be marked sent by the ordinary
customer action, and its provider responses are never written to `catalog.HosterContact` — real
provider statistics never see a sandbox notice.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.public_ids import public_id_field
from marceline.rights.models import ActorKind

#: The kind of public id a notice draft carries (ADR 0033). Named once, because the customer API,
#: the signed bundle's document keys and production's cache lookup all have to agree on it.
NOTICE_ID_PREFIX = "notice"


class NoticeStatus(models.TextChoices):
    DRAFT = "draft"
    CUSTOMER_APPROVED = "customer_approved"
    MARKED_SENT_BY_CUSTOMER = "marked_sent_by_customer"
    PROVIDER_RESPONDED = "provider_responded"
    WITHDRAWN = "withdrawn"
    CORRECTED = "corrected"


#: Statuses a draft can no longer leave.
CLOSED_NOTICE_STATUSES = frozenset({NoticeStatus.WITHDRAWN, NoticeStatus.CORRECTED})


class RenderedVariant(models.TextChoices):
    """The four renderers (answer 01, Part Two "(c)"). One notice, one variant — never merged."""

    DSA_ART16 = "dsa_art16", "DSA Art. 16"
    DMCA_512C = "dmca_512c", "DMCA § 512(c) (hosting)"
    DMCA_512D = "dmca_512d", "DMCA § 512(d) (search delisting)"
    PROVIDER_POLICY = "provider_policy", "Provider copyright channel"


class Capacity(models.TextChoices):
    OWNER = "owner"
    AUTHORISED_AGENT = "authorised_agent", "authorised agent"


class ProviderResponseOutcome(models.TextChoices):
    REMOVED = "removed"
    REFUSED = "refused"
    NO_RESPONSE = "no_response"


class Language(models.TextChoices):
    EN = "en", "English"
    DE = "de", "Deutsch"


class NoticeDraft(models.Model):
    """One canonical draft for one organisation, one recipient provider and one channel.

    Work, claimant and legal-input fields are a **snapshot at drafting time** from
    `marceline.rights` and the confirmed finding: a later rights change or a re-review never
    rewrites an existing draft, only what a new one would capture. `approved_text` /
    `approved_text_sha256` freeze the exact text the customer confirmed (answer 01); the live render before approval is not
    stored, only ever regenerated.
    """

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT, related_name="notice_drafts"
    )
    public_id = public_id_field(NOTICE_ID_PREFIX)
    status = models.CharField(
        max_length=24, choices=NoticeStatus, default=NoticeStatus.DRAFT, db_index=True
    )
    is_sandbox = models.BooleanField(
        editable=False,
        help_text="Snapshot of the organisation's sandbox flag at creation (never changes).",
    )
    language = models.CharField(max_length=8, choices=Language)
    rendered_variant = models.CharField(max_length=24, choices=RenderedVariant)

    # Recipient (answer 01: provider function and channel are a suggestion; the customer adopts
    # one). `complaint_channel` is the directory version used, when the channel came from the
    # directory rather than the RDAP fallback (`catalog.hosting.directory.ChannelView`).
    recipient_hostname = models.CharField(max_length=255)
    recipient_provider_name = models.CharField(max_length=255, blank=True)
    channel_kind = models.CharField(max_length=40, help_text="catalog.models.ComplaintChannel.Kind")
    recipient_email = models.CharField(max_length=254, blank=True)
    recipient_url = models.URLField(max_length=2000, blank=True)
    complaint_channel = models.ForeignKey(
        "catalog.ComplaintChannel",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    forwards_to_site_operator = models.BooleanField(null=True, blank=True)
    publishes_to_transparency_db = models.BooleanField(null=True, blank=True)

    # Work (answer 01 canonical schema, "work").
    sha256 = models.CharField(max_length=64, db_index=True)
    original_public_id = models.CharField(max_length=48, help_text="orig_… at drafting time.")
    work_title = models.CharField(max_length=500)
    legitimate_source_url = models.URLField(max_length=2000, blank=True)
    publication_date = models.DateField(null=True, blank=True)

    # Claimant, snapshotted from `marceline.rights.WorkRights` (answer 01 "claimant").
    claimant_name = models.CharField(max_length=200, blank=True)
    claimant_capacity = models.CharField(max_length=24, choices=Capacity, blank=True)
    underlying_owner_name = models.CharField(max_length=200, blank=True)
    claimant_email = models.CharField(max_length=254, blank=True)
    claimant_address = models.TextField(blank=True)
    claimant_phone = models.CharField(max_length=64, blank=True)
    asserted_right = models.CharField(max_length=32, blank=True)
    asserted_right_detail = models.TextField(blank=True)

    # legal_factual_inputs (answer 01 canonical schema).
    licensed_partner_check_confirmed = models.BooleanField(default=False)
    exception_or_fair_use_confirmed = models.BooleanField(default=False)

    approved_text = models.TextField(blank=True)
    approved_text_sha256 = models.CharField(max_length=64, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    marked_sent_at = models.DateTimeField(null=True, blank=True)
    marked_sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    responded_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=ProviderResponseOutcome, blank=True)
    hoster_contact = models.ForeignKey(
        "catalog.HosterContact",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notice_drafts",
        help_text="The cooperation-history row this response fed (never set for a sandbox draft).",
    )
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawal_reason = models.CharField(max_length=32, blank=True)
    correction_note = models.TextField(blank=True)
    corrected_into = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="corrects"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "notice draft"
        indexes = (models.Index(fields=("organisation", "sha256")),)

    def __str__(self) -> str:
        return (
            f"notice {self.pk}: {self.rendered_variant} to "
            f"{self.recipient_hostname} ({self.status})"
        )


class NoticeFinding(models.Model):
    """One reported URL in a draft: `reported_material[]`, snapshotted at drafting time.

    `included` lets the customer drop a URL from the draft before approval without losing the row
    (the mock-up's unticked checkbox); `exception_confirmed` is the per-URL exception tick the
    answer requires in addition to the batch confirmation whenever the copy is not a current full
    copy (`matching.copy_extent.requires_exception_review`).

    `reviewed_at` is the human-review date the rendered notice names (issue #142). It is a snapshot
    for the same reason every other field here is one: the date is ours, not the recipient's to
    guess, and a notice whose evidence paragraph moved after it was approved would no longer match
    its frozen sha256.
    """

    draft = models.ForeignKey(NoticeDraft, on_delete=models.CASCADE, related_name="findings")
    page = models.ForeignKey("index.Page", on_delete=models.PROTECT, related_name="+")
    url_snapshot = models.URLField(max_length=2000)
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When a human confirmed this copy at band `high` — the earliest such decision "
        "over the file's references (`matching.ReferenceMatch.decided_at`). Snapshotted at "
        "drafting time like every other field here: `approve` freezes a sha256 over the rendered "
        "text, so the text must not depend on data that can move afterwards (issue #142).",
    )
    availability_snapshot = models.CharField(max_length=16)
    extent_snapshot = models.CharField(max_length=32)
    requires_exception_review = models.BooleanField(default=False)
    exception_confirmed = models.BooleanField(default=False)
    included = models.BooleanField(default=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("draft", "page"), name="notice_finding_one_per_page"),
        )

    def __str__(self) -> str:
        return f"finding {self.page_id} of draft {self.draft_id}"


class ConfirmationKind(models.TextChoices):
    RIGHTS = "rights"
    IDENTITY = "identity"
    AUTHORISATION = "authorisation"
    EXCEPTIONS = "exceptions"
    RECIPIENT = "recipient"
    ACCURACY = "accuracy"
    FORWARDING_WARNING = "forwarding_warning"


#: Confirmations required of every draft before it may be approved (answer 01 Part One + Part Two
#: "Version-one implementation specification"). Fixed order; `declarations.py` has the texts.
REQUIRED_CONFIRMATIONS: tuple[str, ...] = (
    ConfirmationKind.RIGHTS,
    ConfirmationKind.IDENTITY,
    ConfirmationKind.AUTHORISATION,
    ConfirmationKind.EXCEPTIONS,
    ConfirmationKind.RECIPIENT,
    ConfirmationKind.ACCURACY,
    ConfirmationKind.FORWARDING_WARNING,
)


class NoticeConfirmation(models.Model):
    """One customer confirmation, stored verbatim with the version it confirmed.

    One row per kind per draft: re-approving after an edit replaces the row (never appends a
    second one for the same kind), because only the latest confirmation for a kind is what the
    customer currently stands behind. The event log keeps the full history of what changed.
    """

    draft = models.ForeignKey(NoticeDraft, on_delete=models.CASCADE, related_name="confirmations")
    kind = models.CharField(max_length=24, choices=ConfirmationKind)
    version = models.CharField(max_length=32)
    language = models.CharField(max_length=8, choices=Language)
    text = models.TextField(help_text="The exact text shown and confirmed, verbatim.")
    confirmed_at = models.DateTimeField(default=timezone.now)
    confirmed_by_id = models.IntegerField(null=True, blank=True, help_text="User id at the time.")
    confirmed_by_label = models.CharField(max_length=150, blank=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("draft", "kind"), name="notice_confirmation_one_per_kind"
            ),
        )

    def __str__(self) -> str:
        return f"{self.kind} confirmation of draft {self.draft_id} ({self.version})"


class NoticeEventKind(models.TextChoices):
    CREATED = "created"
    FINDING_ADDED = "finding_added"
    FINDING_REMOVED = "finding_removed"
    APPROVED = "approved"
    #: The customer downloaded the approved text as a file (ADR 0035 amendment 2026-09-18).
    #: `detail` names the format and repeats `approved_text_sha256` — never a second copy of the
    #: text, which the draft already holds exactly once.
    EXPORTED = "exported"
    MARKED_SENT = "marked_sent"
    SANDBOX_SENT = "sandbox_sent"
    PROVIDER_RESPONSE_RECORDED = "provider_response_recorded"
    WITHDRAWN = "withdrawn"
    CORRECTED = "corrected"


class NoticeEvent(models.Model):
    """One event in a draft's life. Append-only (migration `0011_notices_append_only`).

    `detail` holds codes, ids and — on `approved` — the rendered text's sha256 and language.
    Never the rendered text itself (that lives on the draft, frozen the same moment) and never a
    document's contents.
    """

    draft = models.ForeignKey(NoticeDraft, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=32, choices=NoticeEventKind)
    actor_kind = models.CharField(max_length=16, choices=ActorKind)
    actor_id = models.IntegerField(null=True, blank=True)
    actor_label = models.CharField(max_length=150, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("at", "pk")
        indexes = (models.Index(fields=("draft", "kind", "at")),)

    def __str__(self) -> str:
        return f"{self.kind} on draft {self.draft_id} at {self.at:%Y-%m-%d %H:%M}"
