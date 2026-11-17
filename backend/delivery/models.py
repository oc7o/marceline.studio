"""Tables of the production cache (ADR 0028). Which machine writes which table is by design.

Development (`MARCELINE_ROLE=dev`):
- `ServiceToken` — the bearer credentials the export API accepts, as digests;
- `PurgeOutbox` — purges waiting to reach production;
- `InboxItem` — customer writes pulled from production: what applying each one did, so a pulled
  item is applied once however often it arrives.

Production (`MARCELINE_ROLE=prod`):
- `CachedBundle` / `CachedDocument` — one signed bundle per organisation and the customer API
  responses in it, byte for byte;
- `CacheTombstone` — pages a safety purge removed, so a bundle rendered before the purge can never
  be stored again;
- `PurgeNonce` — nonces of accepted purges (replay protection);
- `ReplicatedAccounts` — when the accounts document was last applied, and the coverage bucket it
  carried;
- `OutboxItem` / `OutboxPullState` — customer writes accepted on production and waiting for
  development to pull and apply them, and when development last pulled (ADR 0028 amendment "The
  production write path").

Both machines run the same migrations; a table the other role writes simply stays empty.
"""

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models

from core.public_ids import public_id_field


class ServiceToken(models.Model):
    """A credential production presents to the development export API.

    Only the SHA-256 digest is stored: tokens are 256-bit random values, so a slow password hash
    adds nothing, and a database dump yields no usable token. `manage.py create_service_token`
    prints the token once; revoking is setting `revoked_at` in the admin.
    """

    class Scope(models.TextChoices):
        EXPORT = "export"
        #: The public host's upload relay (ADR 0044 §9, amendment 2026-09-19): it may append to,
        #: complete, cancel and read an upload it created, and nothing else. Its own scope, so the
        #: read-only export token cannot reach the relay and the relay cannot read a bundle.
        UPLOAD_RELAY = "upload_relay"

    name = models.CharField(max_length=100, unique=True)
    scope = models.CharField(max_length=16, choices=Scope, default=Scope.EXPORT)
    digest = models.CharField(max_length=64, unique=True, help_text="SHA-256 of the token (hex).")
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.scope}{', revoked' if self.revoked_at else ''})"


class CachedBundle(models.Model):
    """The last good bundle of one organisation, as development signed it.

    `organisation_id` is deliberately not a foreign key: the cache must be storable and deletable
    independently of the account replication, and a purge for an organisation production has never
    heard of is a no-op, not an error.
    """

    organisation_id = models.BigIntegerField(unique=True)
    as_of = models.DateTimeField(help_text="When development rendered it (signed).")
    expires_at = models.DateTimeField(help_text="Signed upper bound on serving it at all.")
    fetched_at = models.DateTimeField()
    api_version = models.CharField(max_length=32)
    expired = models.BooleanField(
        default=False, help_text="Set by an `expire` purge: refetch on the next request."
    )
    expire_requested_as_of = models.DateTimeField(
        null=True,
        blank=True,
        help_text="The newest `expire` purge's signed issue time (dev clock). A bundle rendered "
        "at or before it is stored already expired, so a fetch that raced the purge cannot undo "
        "it — the `expire` counterpart of `CacheTombstone`.",
    )
    page_ids = ArrayField(models.BigIntegerField(), default=list, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_failure = models.CharField(max_length=64, blank=True, help_text="A code, never a URL.")

    class Meta:
        indexes = (GinIndex(fields=("page_ids",), name="cachedbundle_page_ids"),)

    def __str__(self) -> str:
        return f"organisation {self.organisation_id} as of {self.as_of:%Y-%m-%d %H:%M:%S}"


class CachedDocument(models.Model):
    """One customer API response inside a bundle: status, content type and the exact body."""

    bundle = models.ForeignKey(CachedBundle, on_delete=models.CASCADE, related_name="documents")
    key = models.CharField(max_length=64)
    status = models.PositiveSmallIntegerField()
    content_type = models.CharField(max_length=100)
    body = models.TextField()

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("bundle", "key"), name="cacheddocument_unique_key"),
        )

    def __str__(self) -> str:
        return f"{self.key} ({self.status})"


class CacheTombstone(models.Model):
    """A page a safety purge removed. Bundles rendered at or before `purged_as_of` that still
    contain it are refused, so a fetch that raced the purge cannot bring the page back."""

    page_id = models.BigIntegerField(unique=True)
    purged_as_of = models.DateTimeField(help_text="The purge's signed issue time (dev clock).")

    def __str__(self) -> str:
        return f"page {self.page_id} purged as of {self.purged_as_of:%Y-%m-%d %H:%M:%S}"


class PurgeNonce(models.Model):
    """A nonce of a signed request from development production has accepted (purges, outbox pulls
    and acknowledgements). Kept a little longer than the clock-skew window."""

    nonce = models.CharField(max_length=64, unique=True)
    issued_at = models.DateTimeField(db_index=True)

    def __str__(self) -> str:
        return self.nonce


class ReplicatedAccounts(models.Model):
    """The single row describing the last accounts document production applied."""

    singleton = models.BooleanField(default=True, unique=True)
    as_of = models.DateTimeField()
    applied_at = models.DateTimeField()
    monitored_sources = models.CharField(max_length=32)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(singleton=True), name="replicatedaccounts_singleton"
            ),
        )

    def __str__(self) -> str:
        return f"accounts as of {self.as_of:%Y-%m-%d %H:%M:%S}"


class PurgeOutbox(models.Model):
    """A purge development owes production. Written in the transaction that caused it, delivered
    after commit, retried by `manage.py send_cache_purges` until production acknowledges."""

    class Mode(models.TextChoices):
        EXPIRE = "expire"
        DELETE = "delete"

    mode = models.CharField(max_length=8, choices=Mode)
    organisation_ids = ArrayField(models.BigIntegerField(), default=list, blank=True)
    page_ids = ArrayField(models.BigIntegerField(), default=list, blank=True)
    reason = models.CharField(max_length=32, help_text="What caused it, as a code.")
    created_at = models.DateTimeField(auto_now_add=True)
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=64, blank=True, help_text="A code, never a URL.")
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = (
            models.Index(
                fields=("created_at",),
                condition=models.Q(sent_at__isnull=True),
                name="purgeoutbox_pending",
            ),
        )

    def __str__(self) -> str:
        return f"{self.mode} ({self.reason}) {'sent' if self.sent_at else 'pending'}"


class OutboxItem(models.Model):
    """A customer write production accepted and development has not settled yet (production).

    Production validated it (session, CSRF, rate limit, the same schema checks as development) and
    answered 202; development pulls it, applies it through the same code as its own endpoint and
    acknowledges the outcome. The customer sees `public_id` (ADR 0033) and a status code, never the
    primary key. `organisation_id` / `user_id` are development's primary keys (accounts replicate
    by them) and deliberately not foreign keys: a pull that replaces accounts must never cascade
    into pending writes.

    `payload` is the validated request body as JSON text — for an original that is hashes and
    metadata only, never file data — and is emptied the moment the item settles; the row itself goes
    `RETENTION` after settling (`outbox_housekeeping`).
    """

    class Kind(models.TextChoices):
        ADD_ORIGINAL = "add_original"
        CLIENT_EVENT = "client_event"
        # Rights writes (ADR 0034): every customer write under `/api/v1/app/rights` except proof
        # document uploads, which stay development-only (the vault never travels through the
        # outbox). `marceline/customer_writes.py` has one handler per kind, each calling the same
        # `marceline.rights.services` function the development endpoint calls.
        RIGHTS_CLAIMANT_TYPE = "rights_claimant_type"
        RIGHTS_ACCOUNT_TOKEN = "rights_account_token"
        RIGHTS_DECLARE = "rights_declare"
        RIGHTS_WITHDRAW = "rights_withdraw"
        RIGHTS_DECLARE_AUTHORITY = "rights_declare_authority"
        RIGHTS_WITHDRAW_AUTHORITY = "rights_withdraw_authority"
        RIGHTS_ADD_PARTNER = "rights_add_partner"
        RIGHTS_REMOVE_PARTNER = "rights_remove_partner"
        RIGHTS_AFFIRM_NO_PARTNERS = "rights_affirm_no_partners"
        # Notice drafts (ADR 0035, ADR 0046): every customer write under `/api/v1/app/notices`
        # except the sandbox send, which is development-only and is the one path that may hand a
        # draft to a mail transport. `NOTICE_EXPORTED` is the audit record of a download the
        # customer made on production — bytes left the cache there, and the append-only
        # `NoticeEvent` log lives on development.
        NOTICE_CREATE = "notice_create"
        NOTICE_APPROVE = "notice_approve"
        NOTICE_MARK_SENT = "notice_mark_sent"
        NOTICE_RESPONSE = "notice_response"
        NOTICE_WITHDRAW = "notice_withdraw"
        NOTICE_CORRECT = "notice_correct"
        NOTICE_EXPORTED = "notice_exported"
        # Onboarding by invite (issue #115, ADR 0052 §9): the one write that happens **before** an
        # account exists, so `organisation_id` and `user_id` are both 0 — there is nobody to name
        # yet. The payload carries the invite's digest (never the token, never the address) and the
        # password hash production made with the project's configured hasher; development applies it
        # with `accounts.invites.apply_redemption`, which ends in the same `complete()` its own
        # endpoint runs. It is the only kind no customer can follow in a submissions list: until it
        # has been applied there is no organisation whose list it could appear in.
        INVITE_REDEEM = "invite_redeem"
        # Account-control verification (ADR 0055): the creator claims the platform account whose
        # content we protect, and says when the code is up. Both are resolved on development, which
        # is where the challenge rows and the codes live; production holds neither.
        VERIFICATION_CLAIM = "verification_claim"
        VERIFICATION_PLACED = "verification_placed"
        # The creator's own data requests (ADR 0056, issue #133): "send me an export" and
        # "delete my data / end my pilot", and taking either back. The row lives on development,
        # where the staff member who answers it works; production holds no request table, so both
        # writes are resolved there. Neither exports nor deletes anything by itself.
        DATA_REQUEST_OPEN = "data_request_open"
        DATA_REQUEST_WITHDRAW = "data_request_withdraw"
        # The mailed account actions (ADR 0054, issue #149). The public host holds no mail transport
        # and no authority over a password: a request enters the outbox (`organisation_id`/`user_id`
        # 0 — a reset is asked for before anybody is signed in), development issues the token and
        # sends the mail, and the **set** carries the new password's hash only. The resend is
        # the one of the four a signed-in customer sends, so it names its own account.
        PASSWORD_RESET_REQUEST = "password_reset_request"
        PASSWORD_RESET_SET = "password_reset_set"
        EMAIL_VERIFY = "email_verify"
        EMAIL_VERIFY_RESEND = "email_verify_resend"

    class Status(models.TextChoices):
        PENDING = "pending"
        DONE = "done"
        REFUSED = "refused"
        QUARANTINED = "quarantined"

    public_id = public_id_field("sub")
    organisation_id = models.BigIntegerField()
    user_id = models.BigIntegerField()
    kind = models.CharField(max_length=32, choices=Kind)
    payload = models.TextField(blank=True)
    payload_bytes = models.PositiveIntegerField(default=0)
    title = models.CharField(
        max_length=300, blank=True, help_text="What the customer sees while it is processed."
    )
    dedupe_key = models.CharField(
        max_length=128, blank=True, help_text="Same key and organisation, still pending: one item."
    )
    received_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    result_code = models.CharField(max_length=64, blank=True, help_text="A code, never text.")
    original_id = models.CharField(
        max_length=64,
        blank=True,
        help_text="The public id of what the write produced, once it exists: an original "
        "(orig_…) for an upload, a notice draft (notice_…) for a notice write.",
    )
    applied_at = models.DateTimeField(
        null=True, blank=True, help_text="When development applied it (signed, dev clock)."
    )
    settled_at = models.DateTimeField(null=True, blank=True)
    pulls = models.PositiveIntegerField(default=0)
    last_pulled_at = models.DateTimeField(null=True, blank=True)
    alerted_at = models.DateTimeField(
        null=True, blank=True, help_text="When its age was reported as an error (once)."
    )

    class Meta:
        indexes = (
            models.Index(
                fields=("id",),
                condition=models.Q(status="pending"),
                name="outboxitem_pending",
            ),
            models.Index(fields=("organisation_id", "received_at"), name="outboxitem_org"),
        )

    def __str__(self) -> str:
        return f"{self.kind} of organisation {self.organisation_id} ({self.status})"


class OutboxPullState(models.Model):
    """The single row saying when development last pulled the outbox (production).

    Production refuses new customer writes when this is older than `OUTBOX_PAUSE_AFTER_HOURS`, so
    a customer is told at once instead of watching a write wait for days."""

    singleton = models.BooleanField(default=True, unique=True)
    last_pull_at = models.DateTimeField()

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(singleton=True), name="outboxpullstate_singleton"
            ),
        )

    def __str__(self) -> str:
        return f"last pull {self.last_pull_at:%Y-%m-%d %H:%M:%S}"


class InboxItem(models.Model):
    """What development did with one pulled customer write (development).

    Written in the transaction that applied it, so an item is applied at most once whatever happens
    to the acknowledgement: a re-pulled item that already has a settled row is only acknowledged
    again. `acknowledged_at` is set once production confirmed the outcome; until then every pull
    run re-sends it. A write that keeps failing is quarantined after `MAX_APPLY_ATTEMPTS` with the
    exception's class as `reason` (never its message, which may quote the payload) and keeps its
    payload so `pull_customer_writes --retry ITEM` can apply it again after a fix.
    """

    class Status(models.TextChoices):
        FAILED = "failed"
        APPLIED = "applied"
        REFUSED = "refused"
        QUARANTINED = "quarantined"

    item_id = models.CharField(max_length=64, unique=True)
    organisation_id = models.BigIntegerField()
    user_id = models.BigIntegerField()
    kind = models.CharField(max_length=32)
    received_at = models.DateTimeField(help_text="When production accepted it.")
    status = models.CharField(max_length=16, choices=Status)
    result_code = models.CharField(max_length=64, blank=True)
    original_id = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=128, blank=True, help_text="Why it failed, as a code.")
    payload = models.TextField(blank=True, help_text="Kept only while quarantined or failing.")
    first_pulled_at = models.DateTimeField()
    settled_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = (
            models.Index(
                fields=("settled_at",),
                condition=models.Q(acknowledged_at__isnull=True, settled_at__isnull=False),
                name="inboxitem_unacknowledged",
            ),
        )

    def __str__(self) -> str:
        return f"{self.item_id} ({self.status})"
