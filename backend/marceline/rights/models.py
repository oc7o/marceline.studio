"""Rights tables (ADR 0034). Only `marceline.rights.services` writes them.

A **work** is one file an organisation owns — every reference with one sha256, the way the customer
app groups originals (ADR 0031). Rights records are keyed by (organisation, sha256), never by a
`Reference` id, so a re-fingerprint keeps its rights and a second organisation claiming the same
file is visible as a competing claim.

Logs are append-only: `WorkRightsTransition` and `RightsEvent` refuse every UPDATE in the database
(migration `0007_rights_logs_append_only`). They are deleted only with the record they describe,
by the retention job.

People are referenced in logs by id and a username snapshot, never by foreign key: a foreign key
with SET_NULL would be an UPDATE on an append-only row, and PROTECT would stop an account from
ever being deleted.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.public_ids import public_id_field
from marceline.rights import account_control


class ClaimantType(models.TextChoices):
    STUDIO_PRODUCER = "studio_producer", "studio / film producer"
    SELF_PRODUCING_CREATOR = "self_producing_creator", "self-producing creator"
    AGENCY = "agency", "agency (authorised agent)"
    STUDIO_PERFORMER = "studio_performer", "performer in a studio production"
    COLLABORATION = "collaboration", "collaboration / co-production"


class IdentityStatus(models.TextChoices):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REVOKED = "revoked"


class IdentityMethod(models.TextChoices):
    COMMERCIAL_REGISTER = "commercial_register", "commercial register extract"
    COMPANY_EMAIL = "company_email", "company email address on the company domain"
    ACCOUNT_CONTROL_TOKEN = "account_control_token", "token placed in a creator-controlled profile"
    # ADR 0055's fallback for doubt and for studios: staff see a document on camera and record only
    # **that** the check happened — never an image, a number or a date of birth.
    VIDEO_CALL = "video_call", "video call, a document seen on camera, nothing recorded"
    OTHER = "other", "other (see evidence note)"


class ActorKind(models.TextChoices):
    STAFF = "staff"
    CUSTOMER = "customer"
    SYSTEM = "system"


class RightsClaimant(models.Model):
    """Who an organisation is as a claimant, and how its identity was checked.

    Identity verification is manual (commercial register, company email, account-control token);
    there is no automated KYC and no identity document is collected by default. Verifying identity
    says who is making an assertion — never that they hold a right.
    """

    organisation = models.OneToOneField(
        "accounts.Organisation",
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="rights_claimant",
    )
    claimant_type = models.CharField(max_length=32, choices=ClaimantType, blank=True)
    claimant_type_set_at = models.DateTimeField(null=True, blank=True)
    identity_status = models.CharField(
        max_length=16, choices=IdentityStatus, default=IdentityStatus.UNVERIFIED
    )
    identity_method = models.CharField(max_length=32, choices=IdentityMethod, blank=True)
    identity_evidence_note = models.TextField(
        blank=True,
        help_text="What staff checked, e.g. the register court and number. No ID document data.",
    )
    identity_verified_at = models.DateTimeField(null=True, blank=True)
    identity_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    account_control_token = models.CharField(
        max_length=64,
        blank=True,
        help_text="Random token the customer places in a profile they control; staff check it.",
    )
    account_control_token_issued_at = models.DateTimeField(null=True, blank=True)
    partners_none_affirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Customer affirmed there are no licensed partners; cleared when one is added.",
    )
    partners_none_affirmed_version = models.CharField(max_length=32, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "rights claimant"

    def __str__(self) -> str:
        return (
            f"claimant of organisation {self.organisation_id} ({self.claimant_type or 'no type'})"
        )


class Capacity(models.TextChoices):
    OWNER = "owner"
    AUTHORISED_AGENT = "authorised_agent", "authorised agent"


class AssertedRight(models.TextChoices):
    COPYRIGHT = "copyright", "copyright / exclusive exploitation right"
    FILM_PRODUCER_RIGHT = "film_producer_right", "film producer's right (§§ 94, 95 UrhG)"
    EXCLUSIVE_LICENCE = "exclusive_licence", "exclusive licence with enforcement right"
    PERFORMER_RIGHT = "performer_right", "performer's right (§§ 77 ff. UrhG)"
    JOINT_RIGHT = "joint_right", "joint right of several holders"
    OTHER = "other", "other (see detail)"


class RightsStatus(models.TextChoices):
    """Where one work's rights record stands. Layers, in the order they are reached.

    `documents_complete` and `authority_declared` are two separate halves:
    **we** record that the documents are formally complete, and **the customer** states that those
    documents give the organisation the right or authority it declared. The second is never ours
    to make — it belongs to the party whose matter it is. `documents_complete` was called `verified` until 2026-09-18, when staff stopped deciding
    what a document supports (ADR 0034 amendment).
    """

    UNVERIFIED = "unverified"
    SELF_DECLARED = "self_declared", "self-declared"
    DOCUMENTS_RECEIVED = "documents_received", "documents received"
    DOCUMENTS_COMPLETE = "documents_complete", "documents complete (formal check)"
    AUTHORITY_DECLARED = "authority_declared", "authority declared by the customer"
    DISPUTED = "disputed"
    REVOKED = "revoked"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


#: Statuses a record can no longer leave on its own: closed for competing claims and retention.
CLOSED_STATUSES = frozenset({RightsStatus.REVOKED, RightsStatus.EXPIRED, RightsStatus.SUPERSEDED})


class RightsSchedule(models.Model):
    """One proof covering many works: a catalogue schedule attached to a contract or licence.

    Works point at the schedule (`WorkRights.schedule`); verifying the schedule verifies each of
    them, one logged transition per work. Every work keeps its own record.
    """

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT, related_name="rights_schedules"
    )
    name = models.CharField(max_length=200)
    description = models.TextField(
        blank=True, help_text="What the schedule covers and which agreement it belongs to."
    )
    valid_until = models.DateField(
        null=True, blank=True, help_text="Empty: the underlying grant is not time-limited."
    )
    legal_hold = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.name} (organisation {self.organisation_id})"


class AgencyMandate(models.Model):
    """An agency's written authority to act for a rights holder.

    `organisation` is the agency (the customer); the principal is the underlying rights holder.
    Revocation and expiry move every linked work to `revoked` / `expired` (`services`).
    """

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT, related_name="agency_mandates"
    )
    principal_name = models.CharField(max_length=200, help_text="Legal name of the rights holder.")
    principal_verified = models.BooleanField(
        default=False,
        help_text="Staff checked the principal's identity, the same formal check as for an owner. "
        "Never a statement about the principal's rights.",
    )
    principal_verification_note = models.TextField(blank=True)
    agent_name = models.CharField(max_length=200, help_text="Agency / legal entity.")
    agent_authorised_person = models.CharField(max_length=200, blank=True)
    whole_catalogue = models.BooleanField(
        default=False, help_text="Covers an objectively defined catalogue, not only listed works."
    )
    catalogue_description = models.TextField(blank=True)
    rights_covered = models.TextField(
        help_text="The rights the document itself names, transcribed. Not an assessment of what "
        "it grants."
    )
    authority_to_prepare_notices = models.BooleanField(
        default=False,
        help_text="The document names notices expressly: it says in its own words that the agent "
        "may prepare or submit takedown notices. A presence check, not a reading of what the "
        "wording covers — if it takes interpretation to answer, leave this unticked.",
    )
    recipient_scope = models.TextField(
        blank=True, help_text="Hosting providers, search providers, platforms."
    )
    territory = models.CharField(
        max_length=200, blank=True, help_text="Empty: the mandate names no territorial limit."
    )
    valid_from = models.DateField()
    valid_until = models.DateField(null=True, blank=True)
    revocation_terms = models.TextField(
        blank=True, help_text="How revocation is communicated and when it takes effect."
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.TextField(blank=True)
    reply_handling = models.BooleanField(
        default=False, help_text="The agent may receive provider responses and counter-notices."
    )
    withdrawal_authority = models.BooleanField(
        default=False, help_text="Express authority to correct or withdraw a mistaken notice."
    )
    sub_authorisation = models.BooleanField(default=False)
    data_disclosure_acknowledged = models.BooleanField(
        default=False,
        help_text="The principal acknowledged that claimant data are sent to notice recipients.",
    )
    signed_by = models.CharField(max_length=200, blank=True)
    signed_on = models.DateField(null=True, blank=True)
    legal_hold = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gte=models.F("valid_from")),
                name="agency_mandate_valid_range",
            ),
        )

    def __str__(self) -> str:
        return f"mandate {self.pk}: {self.agent_name} for {self.principal_name}"


class WorkRights(models.Model):
    """One organisation's rights claim for one work, and where its verification stands.

    At most one current (not superseded) record per organisation and work. A second
    *organisation* holding a current record for the same sha256 is a competing claim: both go to
    `disputed`. `status` changes only through `services.transition`, which writes the log.
    """

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT, related_name="work_rights"
    )
    sha256 = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=24, choices=RightsStatus, default=RightsStatus.UNVERIFIED)
    status_changed_at = models.DateTimeField(default=timezone.now)
    capacity = models.CharField(max_length=24, choices=Capacity, blank=True)
    underlying_owner_name = models.CharField(
        max_length=200, blank=True, help_text="The rights holder, when acting as authorised agent."
    )
    asserted_right = models.CharField(max_length=32, choices=AssertedRight, blank=True)
    asserted_right_detail = models.TextField(blank=True)
    schedule = models.ForeignKey(
        RightsSchedule, null=True, blank=True, on_delete=models.PROTECT, related_name="works"
    )
    mandate = models.ForeignKey(
        AgencyMandate, null=True, blank=True, on_delete=models.PROTECT, related_name="works"
    )
    declaration_version = models.CharField(max_length=32, blank=True)
    declaration_language = models.CharField(max_length=8, blank=True)
    declaration_text = models.TextField(
        blank=True, help_text="The exact text the customer confirmed, as shown."
    )
    declared_at = models.DateTimeField(null=True, blank=True)
    declared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # The customer's second statement (ADR 0034 amendment 2026-09-18): that the documents staff
    # found formally complete give the organisation the right or authority it declared. Only a
    # customer session writes these, and they are cleared whenever the record leaves
    # `authority_declared` — the log keeps what was declared and when.
    authority_declaration_version = models.CharField(max_length=32, blank=True)
    authority_declaration_language = models.CharField(max_length=8, blank=True)
    authority_declaration_text = models.TextField(
        blank=True, help_text="The exact text the customer confirmed, as shown."
    )
    authority_declared_at = models.DateTimeField(null=True, blank=True)
    authority_declared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    legal_hold = models.BooleanField(
        default=False, help_text="Keeps a closed record past its retention date."
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "work rights record"
        verbose_name_plural = "work rights records"
        constraints = (
            models.UniqueConstraint(
                fields=("organisation", "sha256"),
                condition=~Q(status="superseded"),
                name="work_rights_one_current_per_work",
            ),
        )

    def __str__(self) -> str:
        return f"rights {self.pk}: {self.sha256[:12]}… of organisation {self.organisation_id}"


class WorkRightsTransition(models.Model):
    """One status change of a rights record: who, when, from → to, why. Append-only."""

    work = models.ForeignKey(WorkRights, on_delete=models.CASCADE, related_name="transitions")
    from_status = models.CharField(max_length=24, choices=RightsStatus, blank=True)
    to_status = models.CharField(max_length=24, choices=RightsStatus)
    actor_kind = models.CharField(max_length=16, choices=ActorKind)
    actor_id = models.IntegerField(null=True, blank=True, help_text="User id at the time.")
    actor_label = models.CharField(max_length=150, blank=True, help_text="Username at the time.")
    code = models.CharField(
        max_length=32, blank=True, help_text="Machine reason, e.g. competing_claim."
    )
    reason = models.TextField(blank=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("at", "pk")

    def __str__(self) -> str:
        return f"{self.work_id}: {self.from_status or '∅'} → {self.to_status} ({self.actor_kind})"


class PartnerKind(models.TextChoices):
    DOMAIN = "domain"
    PLATFORM = "platform"
    DISTRIBUTOR = "distributor"
    AFFILIATE = "affiliate"


class LicensedPartner(models.Model):
    """A licensed partner or authorised distribution channel of an organisation.

    `domain` (normalised host) and an optional `path_prefix` make it matchable against a URL
    (`partners.partner_for_url`); a distributor without a web presence has only a name.
    """

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.CASCADE, related_name="licensed_partners"
    )
    public_id = public_id_field("partner")
    kind = models.CharField(max_length=16, choices=PartnerKind)
    name = models.CharField(max_length=200)
    domain = models.CharField(max_length=253, blank=True)
    path_prefix = models.CharField(max_length=500, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        indexes = (models.Index(fields=("organisation", "domain")),)

    def __str__(self) -> str:
        return f"{self.name} ({self.kind}{', ' + self.domain if self.domain else ''})"


class ProofKind(models.TextChoices):
    CONTRACT_EXCERPT = "contract_excerpt", "contract excerpt"
    LICENCE = "licence"
    ASSIGNMENT = "assignment"
    AGENCY_MANDATE = "agency_mandate", "agency mandate / power of attorney"
    CATALOGUE_SCHEDULE = "catalogue_schedule", "catalogue schedule"
    COMPANY_REGISTER = "company_register", "company register extract"
    PUBLICATION_RECORD = "publication_record", "original publication / sales account record"
    OTHER = "other"


class ProofDocument(models.Model):
    """A rights-relevant excerpt (PDF or image of a document), encrypted in the vault.

    Never media: a proof is a contract, licence, mandate, schedule or register extract. The bytes
    live encrypted under `RIGHTS_VAULT_DIR` as `storage_name`; this row holds metadata only.
    Staff download through the admin, and every download is a `RightsEvent`.
    """

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT, related_name="rights_proofs"
    )
    public_id = public_id_field("proof")
    kind = models.CharField(max_length=32, choices=ProofKind)
    work = models.ForeignKey(
        WorkRights, null=True, blank=True, on_delete=models.PROTECT, related_name="proofs"
    )
    schedule = models.ForeignKey(
        RightsSchedule, null=True, blank=True, on_delete=models.PROTECT, related_name="proofs"
    )
    mandate = models.ForeignKey(
        AgencyMandate, null=True, blank=True, on_delete=models.PROTECT, related_name="proofs"
    )
    original_filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64, help_text="Digest of the plaintext.")
    storage_name = models.CharField(max_length=64, unique=True, editable=False)
    note = models.TextField(blank=True)
    uploaded_via = models.CharField(max_length=16, choices=ActorKind)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    uploaded_at = models.DateTimeField(default=timezone.now)
    legal_hold = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"proof {self.pk}: {self.kind}, {self.original_filename}"


class RightsEventKind(models.TextChoices):
    CLAIMANT_TYPE_SET = "claimant_type_set"
    IDENTITY_VERIFIED = "identity_verified"
    IDENTITY_REVOKED = "identity_revoked"
    TOKEN_ISSUED = "token_issued"
    PARTNER_ADDED = "partner_added"
    PARTNER_REMOVED = "partner_removed"
    PARTNERS_NONE_AFFIRMED = "partners_none_affirmed"
    PROOF_UPLOADED = "proof_uploaded"
    PROOF_DOWNLOADED = "proof_downloaded"
    MANDATE_REVOKED = "mandate_revoked"
    MANDATE_EXPIRED = "mandate_expired"
    SCHEDULE_EXPIRED = "schedule_expired"


class RightsEvent(models.Model):
    """An organisation-level rights event, including every proof download. Append-only.

    `detail` holds codes and ids only — never document contents, tokens or free-text evidence.
    """

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.PROTECT, related_name="rights_events"
    )
    kind = models.CharField(max_length=32, choices=RightsEventKind)
    proof = models.ForeignKey(
        ProofDocument, null=True, blank=True, on_delete=models.CASCADE, related_name="events"
    )
    mandate = models.ForeignKey(
        AgencyMandate, null=True, blank=True, on_delete=models.CASCADE, related_name="events"
    )
    schedule = models.ForeignKey(
        RightsSchedule, null=True, blank=True, on_delete=models.CASCADE, related_name="events"
    )
    actor_kind = models.CharField(max_length=16, choices=ActorKind)
    actor_id = models.IntegerField(null=True, blank=True)
    actor_label = models.CharField(max_length=150, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("at", "pk")
        indexes = (models.Index(fields=("organisation", "kind", "at")),)

    def __str__(self) -> str:
        return f"{self.kind} for organisation {self.organisation_id} at {self.at:%Y-%m-%d %H:%M}"


class VerificationState(models.TextChoices):
    """Where one claimed platform account stands: `pending` and `placed` are open, the rest
    are settled."""

    PENDING = "pending", "code issued, waiting for the creator"
    PLACED = "placed", "the creator says the code is up"
    VERIFIED = "verified", "staff saw it"
    REJECTED = "rejected", "staff did not see it"
    EXPIRED = "expired", "the code ran out"
    REVOKED = "revoked", "a verification was taken back"


#: States a challenge can no longer leave; a new attempt is a new code on the same row.
SETTLED_VERIFICATION_STATES = frozenset(
    {
        VerificationState.VERIFIED,
        VerificationState.REJECTED,
        VerificationState.EXPIRED,
        VerificationState.REVOKED,
    }
)


class VerificationMethod(models.TextChoices):
    """How a staff member established control (ADR 0055). Recorded, never inferred.

    `staff_legacy` is not a way of checking anything: it is what the status reads for an
    organisation whose identity was recorded through the older admin path
    (`services.verify_identity`, ADR 0034) before or beside this flow. It is never stored on a
    challenge — a database constraint refuses it — so nobody can attribute a real check to it.
    """

    BIO_CODE = "bio_code", "the code was in the account's public bio"
    DM_CODE = "dm_code", "the code arrived by direct message from the account"
    VIDEO_CALL = "video_call", "video call, a document seen on camera, nothing recorded"
    STAFF_LEGACY = "staff_legacy", "recorded through the older admin identity check (ADR 0034)"


#: What a challenge may be decided with. `staff_legacy` is deliberately absent (see above).
CHALLENGE_METHODS = frozenset(
    {VerificationMethod.BIO_CODE, VerificationMethod.DM_CODE, VerificationMethod.VIDEO_CALL}
)


class AccountControlChallenge(models.Model):
    """One organisation's claim to one platform account, and the code that shows it is theirs.

    ADR 0055. The creator claims a platform and a handle, we issue a one-time code, they place it in
    that account's own bio (or send it from it), and a staff member opens the **public** profile by
    hand and records what they saw. Verifying this is what clears the notice gate's
    `identity_not_verified` — through `services.verify_identity`, the one notion of "verified" there
    is (ADR 0034); this model adds no second one.

    **`profile_url` is inert text.** It is displayed to staff, copied by a human, and never fetched:
    no crawler, no HTTP client, no preview (`account_control.py`'s docstring, and a test that fails
    if an HTTP client is imported).

    **No identity document, no date of birth, no age document, ever** — not in `note`, not anywhere
    (operator decision 2026-09-19). The large creator platforms run their own identity
    checks before a creator may publish, so control of such an account is what we check instead.
    """

    organisation = models.ForeignKey(
        "accounts.Organisation", on_delete=models.CASCADE, related_name="account_challenges"
    )
    public_id = public_id_field("chal")
    platform = models.CharField(max_length=32, choices=account_control.PLATFORM_CHOICES)
    handle = models.CharField(
        max_length=account_control.MAX_HANDLE,
        help_text="The account name on that platform, without a leading @.",
    )
    profile_url = models.CharField(
        max_length=account_control.MAX_PROFILE_URL,
        blank=True,
        help_text="Shown to you as text so you can open it yourself. Our code never fetches it.",
    )
    state = models.CharField(
        max_length=16, choices=VerificationState, default=VerificationState.PENDING
    )
    code = models.CharField(
        max_length=32,
        blank=True,
        help_text="The one-time code, in the clear: you compare it by eye with the profile. "
        "Cleared when the challenge is settled.",
    )
    code_issued_at = models.DateTimeField(null=True, blank=True)
    code_expires_at = models.DateTimeField(null=True, blank=True)
    issue_count = models.PositiveSmallIntegerField(
        default=0,
        help_text="How many codes this claim has had. A reissue invalidates the last one.",
    )
    placed_at = models.DateTimeField(null=True, blank=True)
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    method = models.CharField(max_length=16, choices=VerificationMethod, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    note = models.TextField(
        blank=True,
        help_text="Optional. What you saw and where: the platform, that the code matched, the "
        "date. "
        "Never a document number, never a date of birth, never an image — we do not keep those "
        "(ADR 0055).",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "account-control challenge"
        constraints = (
            models.UniqueConstraint(
                fields=("organisation", "platform", "handle"), name="account_challenge_unique_claim"
            ),
            # `staff_legacy` describes the older admin path, so it may never be the method of a real
            # challenge: a check that happened here says which of the three it was.
            models.CheckConstraint(
                condition=~models.Q(method=VerificationMethod.STAFF_LEGACY),
                name="account_challenge_no_legacy_method",
            ),
            # ADR 0055, operator review 2026-09-19: where no platform's own identity check
            # stands behind the account, a code in a bio establishes nothing about who the creator
            # is — so those platforms can only be settled by the video call, and the database says
            # so rather than trusting every future caller of `verify`.
            models.CheckConstraint(
                condition=~models.Q(platform__in=account_control.VIDEO_CALL_ONLY)
                | ~models.Q(state=VerificationState.VERIFIED)
                | models.Q(method=VerificationMethod.VIDEO_CALL),
                name="account_challenge_video_call_only_platforms",
            ),
            # A verified challenge names how and when. Both halves, or the row is misleading.
            models.CheckConstraint(
                condition=~models.Q(state=VerificationState.VERIFIED)
                | (models.Q(decided_at__isnull=False) & ~models.Q(method="")),
                name="account_challenge_verified_is_attributed",
            ),
        )
        indexes = (models.Index(fields=("organisation", "state")),)

    def __str__(self) -> str:
        return (
            f"{self.platform}/{self.handle} of organisation {self.organisation_id} ({self.state})"
        )

    @property
    def settled(self) -> bool:
        return self.state in SETTLED_VERIFICATION_STATES


class AccountControlEventKind(models.TextChoices):
    ISSUED = "issued", "a code was issued (staff)"
    SHOWN = "shown", "the creator asked for their code and it was shown to them"
    PLACED = "placed", "the creator said the code is up"
    VERIFIED = "verified", "staff saw the code on the account"
    REJECTED = "rejected", "staff did not see it"
    EXPIRED = "expired", "the code ran out before anyone looked"
    REVOKED = "revoked", "a verification was taken back"


class AccountControlEvent(models.Model):
    """Every step of one challenge, in order. **Append-only: UPDATE is refused by a trigger**
    (`marceline/0027`), the same technique as `WorkRightsTransition` and `NoticeEvent`.

    `detail` holds codes and short notes only — never the code itself, never a document number, a
    date of birth or anything read off an identity document. The note is optional: no free-text
    field in this flow is ever required, because a required text box is where somebody would type
    the thing we promised not to keep.
    """

    challenge = models.ForeignKey(
        AccountControlChallenge, on_delete=models.CASCADE, related_name="events"
    )
    kind = models.CharField(max_length=16, choices=AccountControlEventKind)
    method = models.CharField(max_length=16, choices=VerificationMethod, blank=True)
    actor_kind = models.CharField(max_length=16, choices=ActorKind)
    actor_id = models.IntegerField(null=True, blank=True)
    actor_label = models.CharField(max_length=150, blank=True)
    note = models.TextField(blank=True)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("at", "pk")
        indexes = (models.Index(fields=("challenge", "at")),)

    def __str__(self) -> str:
        return f"{self.kind} on challenge {self.challenge_id} at {self.at:%Y-%m-%d %H:%M}"
