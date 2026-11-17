"""Users and organisations: who a customer request is scoped to (ADR 0021).

Two segments, one model. **Professional** is an organisation with exactly one user (an independent
creator); **Enterprise** is an organisation with several (a studio or agency). The segment is a
billing and onboarding fact, never a branch in code — every customer query resolves through the
organisation either way, so a solo creator who takes on staff changes plan instead of migrating.

Authentication still uses Django's built-in `auth.User` — **one identity table for staff and
customers**, separated by authorisation rather than by table (ADR 0023). `is_staff` opens the
review API; a `Membership` opens the customer API. Nothing about a staff account and a customer
account differs in how it signs in, which is the point: two identity tables would mean two login
paths, two password flows and two session mechanisms to get right.
"""

from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from accounts import business_status as business_status_rules
from accounts import onboarding
from accounts.declarations import age_declaration_versions, declaration_versions
from core.public_ids import public_id_field


class Organisation(models.Model):
    """The owner of originals in the customer product. One per customer, whatever their size."""

    class Segment(models.TextChoices):
        PROFESSIONAL = "professional"
        ENTERPRISE = "enterprise"

    name = models.CharField(max_length=200)
    segment = models.CharField(
        max_length=16,
        choices=Segment,
        default=Segment.PROFESSIONAL,
        help_text="Billing and onboarding only; it never changes what a request may see.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    #: What the customer API calls this organisation; the primary key never leaves (ADR 0033).
    public_id = public_id_field("org")
    is_sandbox = models.BooleanField(
        default=False,
        db_default=False,
        help_text="Notice drafts (issue #33, ADR 0035): this organisation can never mark a real "
        "send or record a real provider response, and its drafts feed no provider statistics. "
        "The operator's test organisation only; set explicitly here, never inferred.",
    )

    class Meta:
        constraints = (models.UniqueConstraint(Lower("name"), name="organisation_unique_name"),)

    def __str__(self) -> str:
        return f"{self.name} ({self.segment})"

    @property
    def business_status_evidenced(self) -> bool:
        """Has a human looked at objective evidence that this customer is an *Unternehmer*?

        The rule lives in `accounts/business_status.py`; this is the name the rest of the project
        asks by. A sandbox organisation is always true — it is no contracting party (ADR 0035).
        """
        return business_status_rules.business_status_evidenced(self)


class Membership(models.Model):
    """A user's place in an organisation.

    A user belongs to at most one organisation (`membership_one_organisation_per_user`), so
    "which organisation is this request scoped to" has exactly one answer and no request ever has
    to pick. Letting an agency employee serve several customers is a product decision that needs
    an active-organisation concept; until it is taken, the database refuses the ambiguity.

    `role` carries no permissions yet: an owner and a member see the same originals. It exists so
    that seats and invitations (phase 3) have somewhere to land, and so the operator can tell who
    the account holder is.
    """

    class Role(models.TextChoices):
        OWNER = "owner"
        MEMBER = "member"

    organisation = models.ForeignKey(
        Organisation, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=16, choices=Role, default=Role.OWNER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("user",), name="membership_one_organisation_per_user"),
        )

    def __str__(self) -> str:
        return f"{self.user_id} in {self.organisation_id} ({self.role})"

    def clean(self) -> None:
        """Refuse a membership for an organisation whose business status is not evidenced —
        but only where the operator has asked for that (issue #92).

        A membership *is* the grant of customer access: `GET /api/v1/app/me` reports
        `can_add_originals` exactly when one exists (`marceline/app/account.py`), so this is the
        staff-side step the evidence has to be on file for.

        `ONBOARDING_BUSINESS_STATUS_GUARD` decides, and it defaults to `warn` — in which case this
        method does nothing and the admin shows a message instead. Both admin paths (the
        `Membership` screen and the inline on the organisation) run `full_clean` through their
        form, so `refuse` reaches both without either of them knowing about it. `seed_test_customer`
        and `seed_demo_dataset` create their rows with `get_or_create`, which does not call
        `full_clean`, so they keep working whatever the setting says.
        """
        super().clean()
        if self.organisation_id is None:
            return
        try:
            business_status_rules.check_before_access(self.organisation)
        except business_status_rules.BusinessStatusNotEvidenced as refusal:
            raise ValidationError({"organisation": str(refusal)}) from refusal


class BusinessStatus(models.Model):
    """Why we believe this customer is an *Unternehmer*, and who looked (issue #92).

    The pilot is B2B-only and EU-only (operator decision, issue #95 item 7). A declaration alone
    does not make that true; what counts is the objective evidence seen at the time. This row is
    that record: what we saw, who saw it, when, and where the business sits — plus the declaration the
    customer themselves signed, with the verbatim wording and its version.

    **This model records; `accounts/business_status.py` judges.** Saving a half-filled row is
    allowed on purpose — onboarding is a conversation, and a form that refuses a partial answer
    teaches staff to write something untrue into it. What is missing is reported as codes, and the
    guard on membership creation is where it has consequences. `clean()` only enforces the rules
    that would make a *stored* row misleading.

    **No identity documents.** The same rule as `RightsClaimant` (ADR 0034): staff record *that*
    they saw a document and enough to find it again, never its contents and never a copy. The VAT
    identification number has its own field because it is the one number that is genuinely business
    data — it identifies an undertaking, not a person, it is published for verification in VIES,
    and giving it a field is what keeps it out of the free-text note where document numbers about
    people would otherwise accumulate.

    Accounts may depend on `core` only, and this needs nothing else: an organisation, a user and
    the declaration texts next door. The rights tables in `marceline/rights/` are about works, not
    about who the contracting party is, and an app may never import `marceline`.
    """

    class EvidenceKind(models.TextChoices):
        COMMERCIAL_REGISTER = "commercial_register", "commercial register extract seen"
        VAT_ID = "vat_id", "VAT identification number checked"
        TRADE_REGISTRATION = "trade_registration", "trade registration seen (Gewerbeanmeldung)"
        BUSINESS_PROFILE = "business_profile", "professional profile or imprint of the business"
        OTHER = "other", "other (say what in the evidence note)"

    organisation = models.OneToOneField(
        Organisation,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="business_status",
    )
    evidence_kind = models.CharField(max_length=32, choices=EvidenceKind, blank=True)
    evidence_note = models.TextField(
        blank=True,
        help_text=(
            "What was checked, in enough detail to find it again — e.g. the register court and "
            "number, or which imprint. No identity document data, and no document number beyond "
            "the one that identifies the business record itself. A VAT ID goes in its own field."
        ),
    )
    vat_id = models.CharField(
        max_length=20,
        blank=True,
        help_text=(
            "VAT identification number, e.g. DE123456789. Business data about an undertaking, "
            "publicly verifiable; stored deliberately. Spaces are removed and it is upper-cased."
        ),
    )
    country = models.CharField(
        max_length=2,
        blank=True,
        help_text=(
            "Where the business is established, as an ISO 3166-1 alpha-2 code. The pilot is "
            "EU-only (issue #95); a country outside the EU is recorded and flagged, never refused."
        ),
    )
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The staff member who looked at the evidence.",
    )
    checked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When they looked. Without it the organisation counts as not evidenced.",
    )
    declaration_version = models.CharField(
        max_length=40,
        blank=True,
        help_text="Which version of the Unternehmer declaration the customer signed.",
    )
    declaration_text = models.TextField(
        blank=True,
        help_text="The wording they signed, verbatim. A snapshot: it never changes with the file.",
    )
    declared_at = models.DateTimeField(null=True, blank=True)
    # The declaration of majority and full legal capacity (issue #123). A second field group rather
    # than a flag: like the Unternehmer one it is a *wording somebody agreed to*, so the version and
    # the verbatim text are the record, and the moment is what makes it evidence. **No date of
    # birth** — the declaration is the record (Art. 5(1)(c) GDPR).
    age_declaration_version = models.CharField(
        max_length=40,
        blank=True,
        help_text="Which version of the majority declaration the account holder signed.",
    )
    age_declaration_text = models.TextField(
        blank=True,
        help_text="The wording they signed, verbatim. A snapshot: it never changes with the file.",
    )
    age_declared_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the account holder declared being of full age and of legal capacity.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "business status"
        verbose_name_plural = "business statuses"
        constraints = (
            # A version without its text would be a declaration nobody can read back, and a text
            # without a version one nobody can place. All three or none.
            models.CheckConstraint(
                condition=(
                    models.Q(declaration_version="", declaration_text="", declared_at__isnull=True)
                    | (
                        ~models.Q(declaration_version="")
                        & ~models.Q(declaration_text="")
                        & models.Q(declared_at__isnull=False)
                    )
                ),
                name="business_status_declaration_complete",
            ),
            # The same all-three-or-none rule for the majority declaration (issue #123).
            models.CheckConstraint(
                condition=(
                    models.Q(
                        age_declaration_version="",
                        age_declaration_text="",
                        age_declared_at__isnull=True,
                    )
                    | (
                        ~models.Q(age_declaration_version="")
                        & ~models.Q(age_declaration_text="")
                        & models.Q(age_declared_at__isnull=False)
                    )
                ),
                name="business_status_age_declaration_complete",
            ),
            models.CheckConstraint(
                condition=models.Q(country="") | models.Q(country__regex=r"^[A-Z]{2}$"),
                name="business_status_country_is_iso_alpha2",
            ),
        )

    def __str__(self) -> str:
        return f"business status of organisation {self.organisation_id}"

    def clean(self) -> None:
        super().clean()
        self.country = self.country.strip().upper()
        self.vat_id = "".join(self.vat_id.split()).upper()
        errors: dict[str, str] = {}
        if self.country and not business_status_rules.COUNTRY_CODE.match(self.country):
            errors["country"] = "Two letters, ISO 3166-1 alpha-2, e.g. DE."
        if self.evidence_kind == self.EvidenceKind.OTHER and not self.evidence_note.strip():
            errors["evidence_note"] = "Say what was checked when the kind is 'other'."
        if self.evidence_kind == self.EvidenceKind.VAT_ID and not self.vat_id.strip():
            errors["vat_id"] = "Record the VAT ID that was checked."
        if self.declaration_version and self.declaration_version not in declaration_versions():
            errors["declaration_version"] = (
                "Unknown version. The published wordings are in accounts/declarations.py; a new "
                "wording is a new version there, never an edit of an old one."
            )
        if self.declaration_version and not self.declaration_text.strip():
            errors["declaration_text"] = "Store the wording that was signed, verbatim."
        if self.declaration_version and self.declared_at is None:
            errors["declared_at"] = "When was it signed?"
        if (
            self.age_declaration_version
            and self.age_declaration_version not in age_declaration_versions()
        ):
            errors["age_declaration_version"] = (
                "Unknown version. The published wordings are in accounts/declarations.py; a new "
                "wording is a new version there, never an edit of an old one."
            )
        if self.age_declaration_version and not self.age_declaration_text.strip():
            errors["age_declaration_text"] = "Store the wording that was signed, verbatim."
        if self.age_declaration_version and self.age_declared_at is None:
            errors["age_declared_at"] = "When was it signed?"
        if errors:
            raise ValidationError(errors)

    def codes(self) -> list[str]:
        """What is missing or noteworthy, in `business_status.CODES` order.

        `NOT_CHECKED` keys on `checked_at` alone: `checked_by` is `SET_NULL`, so a staff account
        deleted years later must not turn an evidenced organisation back into an unevidenced one.
        """
        rules = business_status_rules
        found = set()
        if not self.evidence_kind:
            found.add(rules.NO_EVIDENCE_KIND)
        elif self.evidence_kind == self.EvidenceKind.OTHER and not self.evidence_note.strip():
            found.add(rules.EVIDENCE_NOTE_MISSING)
        elif self.evidence_kind == self.EvidenceKind.VAT_ID and not self.vat_id.strip():
            found.add(rules.VAT_ID_MISSING)
        if self.checked_at is None:
            found.add(rules.NOT_CHECKED)
        if not self.country:
            found.add(rules.COUNTRY_MISSING)
        elif self.country not in rules.EU_MEMBER_STATES:
            found.add(rules.OUTSIDE_EU)
        if not self.declaration_version:
            found.add(rules.DECLARATION_MISSING)
        if not self.age_declaration_version:
            found.add(rules.AGE_DECLARATION_MISSING)
        return [code for code in rules.CODES if code in found]


class LoginAttempt(models.Model):
    """One failed sign-in, counted so that the next one can be refused (ADR 0023).

    A login endpoint on the public internet needs per-account and per-IP limits that hold across
    processes: gunicorn runs several workers, so an in-process counter (the one `review/media.py`
    uses for image passthrough) would let an attacker multiply their budget by the number of
    workers and reset it with a deploy. This table is the shared counter, and Postgres is already
    the project's queue and state store (ADR 0002) — no Redis for this.

    **The row identifies nobody.** `key` is an HMAC-SHA256 digest of the scope and the submitted
    value under `SECRET_KEY`, never the username or the IP address themselves. Counting only needs
    equality, so storing the plaintext would add a log of who tried to sign in and from where —
    exactly the kind of record a customer product should not accumulate (issue #49). A digest is
    also why an unknown username is throttled exactly like a real one: nothing here knows, or can
    learn, whether the account exists.

    A row is written when an attempt is admitted (so a parallel burst is counted as it happens),
    taken back with the account's bucket on success, and purged once it is older than the counting
    window plus the lockout. `accounts/login_throttle.py` owns all of that; nothing else may write
    this table.
    """

    class Scope(models.TextChoices):
        ACCOUNT = "account"
        IP = "ip"

    scope = models.CharField(max_length=16, choices=Scope)
    key = models.CharField(
        max_length=64,
        help_text="HMAC-SHA256 of the scope and value under SECRET_KEY — never the value itself.",
    )
    # Not auto_now_add: the throttle reasons about time explicitly, and tests place attempts in
    # the past rather than sleeping through a fifteen-minute window.
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = (models.Index(fields=("key", "created_at")),)

    def __str__(self) -> str:
        return f"{self.scope} {self.key[:12]}… at {self.created_at:%Y-%m-%d %H:%M:%S}"


def default_invite_expiry() -> datetime:
    """`ONBOARDING_INVITE_EXPIRY_DAYS` from now (issue #115, ADR 0052).

    A model default rather than something the caller computes, so the admin's add form prefills the
    date the operator is agreeing to and `manage.py invite_customer` and a test all get the same
    answer from one place.
    """
    return timezone.now() + timedelta(days=onboarding.expiry_days())


class CustomerInvite(models.Model):
    """A staff-issued way in for one creator (ADR 0052, issue #115).

    **This is not signup.** ADR 0023's decision stands: no stranger can create an account. What
    changes is who does the clerical part — staff issue an invite for a person they have already
    written to, and that person sets their own password, signs the *Unternehmer* and the majority
    declarations and accepts the documents themselves, instead of the operator typing all of it
    into the admin.

    **The token is never stored.** `token_hash` is a SHA-256 of it (`accounts/onboarding.py`); the
    token is printed once at issue and cannot be recovered. Looking an invite up is one indexed
    query on that digest, which is also what makes an unknown token cost the same as a real one.

    **Nothing is created until the last step.** A password set at step one is held here as a Django
    password hash, a declaration is held here verbatim, and the user, organisation, membership and
    `BusinessStatus` appear in the single transaction that stamps `redeemed_at`
    (`accounts/invites.py::complete`). An abandoned invite therefore leaves no half-built account
    on the identity table — this row is the only thing to clean up.

    **The step is derived, never stored** (`accounts/onboarding.Step`): a cursor and the fields it
    claims to describe can disagree, and then one of them is a lie.
    """

    #: Either an existing organisation, or a name to create one under — never both, never neither
    #: (`customer_invite_one_destination`).
    organisation = models.ForeignKey(
        Organisation,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="invites",
        help_text="An existing organisation to join. Leave empty to create one from the name.",
    )
    organisation_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="The organisation to create on redemption. Empty when joining an existing one.",
    )
    segment = models.CharField(
        max_length=16,
        choices=Organisation.Segment,
        default=Organisation.Segment.PROFESSIONAL,
        help_text="Only used when an organisation is created; billing and onboarding only.",
    )
    email = models.EmailField(
        help_text="The address the invite goes to. It becomes the account's username and is "
        "fixed by the invite: the person redeeming it cannot choose another one."
    )
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 of the token. The token itself is shown once and never stored.",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The staff member who issued it.",
    )
    note = models.TextField(
        blank=True,
        help_text="Why this invite exists, for staff. No contact details beyond the address above "
        "— a note is not a place to accumulate personal data about somebody who has no account.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        default=default_invite_expiry,
        help_text="After this, the link answers the neutral page — like every other failure.",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(
        max_length=200,
        blank=True,
        help_text="Staff-only. Never shown to whoever holds the link: every failure looks alike.",
    )
    redeemed_at = models.DateTimeField(null=True, blank=True)
    redeemed_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The account this invite produced.",
    )

    # --- the half-finished redemption, held here until `complete` moves it onto real rows -------
    password_hash = models.CharField(
        max_length=128,
        blank=True,
        help_text="Django's own password hash, made with the configured hasher. Never a password.",
    )
    declaration_version = models.CharField(max_length=40, blank=True)
    declaration_text = models.TextField(
        blank=True,
        help_text="The wording that was shown, verbatim — a snapshot, as in BusinessStatus.",
    )
    declared_at = models.DateTimeField(null=True, blank=True)
    age_declaration_version = models.CharField(max_length=40, blank=True)
    age_declaration_text = models.TextField(
        blank=True,
        help_text="The majority wording that was shown, verbatim — a snapshot, as in "
        "BusinessStatus. No date of birth is asked for or stored (issue #123).",
    )
    age_declared_at = models.DateTimeField(null=True, blank=True)
    country = models.CharField(
        max_length=2,
        blank=True,
        help_text="Where the business is established, ISO 3166-1 alpha-2. Only ever an EU member "
        "state: the pilot is EU-only (issue #95), and an answer outside it is counted, not stored.",
    )
    outside_eu_answers = models.PositiveIntegerField(
        default=0,
        db_default=0,
        help_text="How often this invite was answered with a country outside the EU. A count and "
        "nothing else — we have no account and no contract with this person, so which country it "
        "was is not ours to keep (ADR 0052 §5). The invite is not consumed by such an answer.",
    )

    # --- production only: the redemption handed to the outbox (ADR 0052 §9, slice 2) -------------
    #
    # On the public host no account may be created (ADR 0028): the finished redemption is enqueued
    # as a `delivery.OutboxItem` and development applies it. These two fields are what makes single
    # use hold **on that host**, before development has seen anything: once `handed_over_at` is set,
    # `lookup()` no longer finds the invite, so no step can be answered again and no second item can
    # ever be enqueued. Both stay empty on development, where `complete()` redeems in one
    # transaction; the replication never writes them (`marceline/accounts_replica.py`).
    handed_over_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Production only: when the finished redemption was accepted into the outbox. "
        "The link answers the neutral page from then on — like every other failure.",
    )
    submission_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="Production only: the outbox item carrying the redemption (sub_…).",
    )

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=(
                    models.Q(organisation__isnull=False, organisation_name="")
                    | (models.Q(organisation__isnull=True) & ~models.Q(organisation_name=""))
                ),
                name="customer_invite_one_destination",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(declaration_version="", declaration_text="", declared_at__isnull=True)
                    | (
                        ~models.Q(declaration_version="")
                        & ~models.Q(declaration_text="")
                        & models.Q(declared_at__isnull=False)
                    )
                ),
                name="customer_invite_declaration_complete",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        age_declaration_version="",
                        age_declaration_text="",
                        age_declared_at__isnull=True,
                    )
                    | (
                        ~models.Q(age_declaration_version="")
                        & ~models.Q(age_declaration_text="")
                        & models.Q(age_declared_at__isnull=False)
                    )
                ),
                name="customer_invite_age_declaration_complete",
            ),
        )
        indexes = (models.Index(fields=("expires_at",)),)

    def __str__(self) -> str:
        return f"invite {self.pk} ({self.token_hash[:12]}…)"

    @property
    def destination(self) -> str:
        """Where this invite leads, for a staff screen. Never the address."""
        if self.organisation_id is not None:
            return f"organisation {self.organisation_id}"
        return f"new organisation “{self.organisation_name}”"


class DocumentAcceptance(models.Model):
    """One document one person accepted, at one moment (ADR 0052 §3).

    **Append-only**: nothing in the project updates or deletes a row here, and the redemption
    writes one row per document per acceptance. A second acceptance of the same document is a
    second row, so "what did they agree to, and when" always has an answer that does not depend on
    the file that happens to be published today.

    What is stored is the document's id, its version, the **sha256 of the published text** the link
    resolved to, the language it was presented in and the moment — never the paragraphs. The text
    is a published artefact addressed by its digest; a stored digest that matches nothing published
    any more is itself the finding, whereas a copy in every row would invite editing.

    The invite, the user and the organisation are all nullable `SET_NULL`: a deleted invite or a
    closed account must not take the record of an acceptance with it.
    """

    invite = models.ForeignKey(
        CustomerInvite,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="acceptances",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="document_acceptances",
    )
    organisation = models.ForeignKey(
        Organisation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="document_acceptances",
    )
    document_id = models.CharField(max_length=64)
    version = models.CharField(max_length=40)
    sha256 = models.CharField(max_length=64, help_text="Of the published text that was presented.")
    url = models.URLField(blank=True, help_text="Where that text was published.")
    language = models.CharField(max_length=8, blank=True)
    accepted_at = models.DateTimeField()

    class Meta:
        verbose_name = "document acceptance"
        verbose_name_plural = "document acceptances"
        indexes = (
            models.Index(fields=("document_id", "version")),
            models.Index(fields=("user", "accepted_at")),
        )

    def __str__(self) -> str:
        return f"{self.document_id} {self.version} at {self.accepted_at:%Y-%m-%d %H:%M:%S}"


class AccountActionToken(models.Model):
    """A single-use link development mailed to one account (ADR 0054, issue #149).

    Two kinds, one row shape: the password reset and the address verification. Everything that makes
    the invite safe applies here for the same reasons (ADR 0052 §1, §6):

    - **the token is never stored**, only its SHA-256 (`accounts/action_tokens.py`). It is generated
      on development, put in exactly one mail, and cannot be recovered from any row afterwards;
    - **single use**: `used_at` on development, `handed_over_at` on the public host, where the write
      goes into the outbox instead (ADR 0028) and the link stops working the moment it is enqueued —
      the same marker, the same reason, as `CustomerInvite.handed_over_at`;
    - **one refusal**: unknown, expired, used, handed over and "the feature is off" are one answer.
      A second sentence would say whether an address has an account.

    The user is the authority, not the address: a reset sets `User.password` for **this row's
    user**,
    so an address that changed between the request and the redemption cannot move the credential.
    """

    class Kind(models.TextChoices):
        PASSWORD_RESET = "password_reset"
        EMAIL_VERIFICATION = "email_verification"

    kind = models.CharField(max_length=24, choices=Kind)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="action_tokens"
    )
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 of the token. The token itself exists only in the mail that carried it.",
    )
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    mailed_at = models.DateTimeField(
        null=True, blank=True, help_text="When the mail was handed to the transport."
    )
    used_at = models.DateTimeField(
        null=True, blank=True, help_text="When it was redeemed (development)."
    )
    handed_over_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Production only: when the write it carries was accepted into the outbox. The "
        "link answers the neutral page from then on — like every other failure.",
    )
    submission_id = models.CharField(
        max_length=64, blank=True, help_text="Production only: the outbox item (sub_…)."
    )

    class Meta:
        indexes = (
            models.Index(fields=("user", "kind", "created_at")),
            models.Index(fields=("expires_at",)),
        )

    def __str__(self) -> str:
        return f"{self.kind} for user {self.user_id} (expires {self.expires_at:%Y-%m-%d %H:%M})"


class EmailVerification(models.Model):
    """Whether one account's address has been confirmed by whoever reads it (ADR 0054).

    A row rather than a column because `AUTH_USER_MODEL` is `auth.User` and this project does not
    add a second identity table (`backend/marceline/CLAUDE.md`, ADR 0023): one fact about the
    address, one-to-one with the account, created when the first verification mail goes out.

    **Unverified blocks nothing in the beta** (ADR 0054): staff issued the invite *to* this address,
    so it is already as good as the operator's own record of it, and refusing a signed-in customer
    their own screens over an unopened mail would be a worse lie than the banner that asks. What it
    does change is what we may rely on later — a notice's reply-to, a deletion confirmation — and
    those read `verified_at`, never the flag.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_verification"
    )
    verified_at = models.DateTimeField(
        null=True, blank=True, help_text="When the address was confirmed from a mailed link."
    )
    last_sent_at = models.DateTimeField(
        null=True, blank=True, help_text="When the last verification mail went out (rate limiting)."
    )

    class Meta:
        verbose_name = "email verification"
        verbose_name_plural = "email verifications"

    def __str__(self) -> str:
        state = "verified" if self.verified_at is not None else "unverified"
        return f"user {self.user_id}: {state}"
