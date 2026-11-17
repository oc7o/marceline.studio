"""Sources we monitor, creators we look for, and content-safety terms."""

import re
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models

from catalog import do_not_contact
from catalog.hosting.providers import is_role_address, parse_languages
from catalog.sources import AGE_CONFIRMATION_HELP, AgeConfirmationError, parse_age_confirmation
from core import deletion_journal


class Site(models.Model):
    """A piracy site on the crawl allowlist."""

    domain = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=True)
    crawl_delay_s = models.FloatField(default=5.0, help_text="Minimum seconds between requests.")
    robots_crawl_delay_s = models.FloatField(
        null=True, blank=True, help_text="Crawl-delay from robots.txt, recorded by the crawler."
    )
    # Request budgets (issue #74, ADR 0007 amendment 2026-09-17): real requests per rolling 24 h,
    # keyed by (site, host role, request kind). The site's own servers are expensive, so origin
    # budgets are strict and split by kind; CDN hosts have their own, higher, per-host budgets
    # (`SiteHost.daily_request_budget`, default `CDN_HOST_DAILY_REQUEST_BUDGET`).
    daily_request_budget = models.PositiveIntegerField(
        default=1000,
        help_text="Origin page budget: real requests per rolling 24 hours to the site's own hosts "
        "for pages and robots.txt, retries and redirect hops included.",
    )
    daily_media_request_budget = models.PositiveIntegerField(
        default=1000,
        help_text="Origin media budget: real requests per rolling 24 hours for media served from "
        "the site's own hosts, retries and redirect hops included. Media on CDN hosts counts "
        "against the CDN host's budget instead (Site hosts).",
    )
    # Site outage (review M6): written by the crawler, not by hand. While paused no task of the
    # site is claimed, so an outage costs no task its attempts and robots.txt is asked once per
    # pause window instead of once per task.
    paused_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="The crawler claims no task of this site before this time (outage backoff).",
    )
    pause_reason = models.TextField(blank=True, help_text="The failure that paused the site.")
    consecutive_failures = models.PositiveIntegerField(
        default=0, help_text="Connection or robots.txt failures in a row; reset by any response."
    )
    extraction = models.JSONField(
        default=dict, blank=True, help_text="Per-site extraction rules (LLM-generated later)."
    )
    age_confirmation = models.JSONField(default=dict, blank=True, help_text=AGE_CONFIRMATION_HELP)
    # Browser capture (ADR 0016, issue #78): an operator decision, not a reviewed crawl policy, so
    # it is edited here directly and is never part of `catalog.sources.SitePolicy` / re-applied by
    # `seed_sources` (like `age_confirmation` above) — a seed run must never silently switch a
    # site back on. Default True: no site is excluded until the operator decides one is.
    capture_enabled = models.BooleanField(
        default=True,
        help_text="Whether browser capture (ADR 0016) may visit this site's pages. Off for a "
        "site the operator has decided cannot be captured within policy — the reason lives in "
        "docs/research/browser-capture-no-video.md, never here.",
    )
    # The one interaction browser capture may perform (ADR 0016 amendment 2026-09-18, issue #78):
    # a single click on the page's own play control, on a site the operator approved for it.
    # Default False — no site gets it without a decision. Like `capture_enabled` and
    # `age_confirmation` above, it is deliberately not part of `catalog.sources.SitePolicy`, so a
    # `seed_sources` run can never switch it on or off behind the operator's back.
    capture_play_click = models.BooleanField(
        default=False,
        help_text="Whether browser capture may click this site's own play control, once per "
        "visit (ADR 0016 amendment 2026-09-18). Only for a site that builds its player on a "
        "click; which element may be clicked is decided in marceline/capture/click.py, never "
        "here. Never a consent, age, login, subscribe, download or ad control.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("domain",)

    def __str__(self) -> str:
        return self.domain

    def clean(self) -> None:
        try:
            parse_age_confirmation(self.age_confirmation)
        except AgeConfirmationError as exc:
            raise ValidationError({"age_confirmation": str(exc)}) from exc


class CreatorManager(models.Manager["Creator"]):
    """`usable()` is the one answer to "may we act on this name at all"."""

    def usable(self) -> models.QuerySet["Creator"]:
        """Creators we may hunt, link to pages, search for or report on.

        Two exclusions, both deliberate and both enforced here rather than at each call site:
        a name a customer typed that staff have not reviewed (`needs_review`, issue #29), and a
        name under an active index suppression (`CreatorSuppression`, docs/suppression.md) —
        someone who asked us to stop indexing them. Every reading path that hunts, links,
        searches or reports goes through this queryset.
        """
        return self.filter(needs_review=False).exclude(
            pk__in=CreatorSuppression.objects.filter(lifted_at__isnull=True).values("creator_id")
        )


class Creator(models.Model):
    """A creator whose content we search for.

    `needs_review` marks a name a customer typed when adding an original in the app (issue #29)
    that matched no existing creator. It is a claim, not a curated record: until staff have looked
    at it (admin action "Mark reviewed"), it is never a crawl target, never linked to crawled pages
    and never used for source discovery, so a typo or a name the customer has no rights to cannot
    steer the crawler. Staff merge it into an existing creator by hand if it is a duplicate.

    A `CreatorSuppression` row (docs/suppression.md) takes the name out of everything we do:
    `Creator.objects.usable()` is what every hunting, linking, discovery and reporting path reads.
    """

    name = models.CharField(max_length=255, unique=True)
    aliases = models.JSONField(default=list, blank=True)
    needs_review = models.BooleanField(
        default=False,
        help_text="Named by a customer and not checked by staff yet: not crawled, not linked to "
        "pages, not used for discovery until reviewed.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CreatorManager()

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    @property
    def suppressed(self) -> bool:
        """Is this name under an active index suppression (docs/suppression.md)?"""
        suppression = getattr(self, "suppression", None)
        return suppression is not None and suppression.lifted_at is None


class CreatorSuppression(models.Model):
    """A name we must not index: the performer asked us to stop (docs/suppression.md).

    The durable marker behind the route for a performer who is **not** our customer and objects to
    being indexed (GDPR Art. 21, or an erasure request under Art. 17). It survives re-crawls: while
    a row exists with `lifted_at` unset, the name is out of `Creator.objects.usable()`, so nothing
    seeds it, links it to a page, searches for it or reports on it, and a database trigger
    (`index/migrations/0021_suppressed_creator_links`) drops any page-creator link a writer still
    tries to store. The row is the "minimal record kept so we do not re-add them" — that is why it
    is kept after the pages, links and breadcrumbs are gone, and why deleting the creator is
    refused (`PROTECT`).

    **What may go in here:** the name (through the creator), dates, how identity was checked as a
    code, and a short staff note. **Never** contact details, a copy of the request, an identity
    document or anything else the requester sent — that correspondence stays in the operator's
    mailbox (docs/suppression.md, "What we record").
    """

    class Basis(models.TextChoices):
        OBJECTION = "objection", "Objection to our legitimate interest (Art. 21 GDPR)"
        ERASURE = "erasure", "Erasure request (Art. 17 GDPR)"
        OPERATOR = "operator", "Our own decision (no request from the person)"
        OTHER = "other", "Other — see the note"

    class IdentityCheck(models.TextChoices):
        """How the requester's identity was checked. Paperwork only — never a measurement.

        We never compare faces, voices or any other biometric feature to decide who someone is
        (CLAUDE.md hard rules): identity here is checked by documents and channels, by a person.
        """

        PLATFORM_MESSAGE = "platform_message", "Message from an account that publishes the name"
        STUDIO_OR_AGENCY = "studio_or_agency", "Confirmed by the studio or agency of record"
        COUNSEL = "counsel", "Requested through a lawyer or agency with a mandate"
        DOCUMENT_SEEN = "document_seen", "Identity document seen by a person, never copied here"
        NOT_VERIFIED = "not_verified", "Not verified; suppressed anyway (the cautious answer)"
        OTHER = "other", "Other — see the note"

    creator = models.OneToOneField(
        Creator,
        on_delete=models.PROTECT,
        related_name="suppression",
        help_text="The name, with its aliases, that must not be indexed.",
    )
    basis = models.CharField(max_length=16, choices=Basis, default=Basis.OBJECTION)
    requested_on = models.DateField(help_text="When the request reached us (day precision).")
    answer_due_on = models.DateField(
        help_text="When our answer is due: one month from the request (Art. 12(3) GDPR)."
    )
    answered_on = models.DateField(
        null=True, blank=True, help_text="When we answered the requester."
    )
    identity_check = models.CharField(
        max_length=24,
        choices=IdentityCheck,
        default=IdentityCheck.NOT_VERIFIED,
        help_text="How identity was checked — a code, never a measurement.",
    )
    identity_note = models.TextField(
        blank=True,
        help_text="What was checked, in one line. No contact details, no document contents.",
    )
    note = models.TextField(
        blank=True, help_text="Anything else staff need. No contact details, no request text."
    )
    decided_by = models.CharField(
        max_length=150, blank=True, help_text="Who applied it (username or 'operator')."
    )
    applied_at = models.DateTimeField(
        null=True, blank=True, help_text="When the suppression was last applied to stored data."
    )
    removed_page_links = models.PositiveIntegerField(
        default=0, help_text="Page-creator links deleted so far (all runs)."
    )
    removed_breadcrumbs = models.PositiveIntegerField(
        default=0, help_text="Site x creator breadcrumb rows deleted so far."
    )
    redacted_pages = models.PositiveIntegerField(
        default=0, help_text="Pages whose stored extraction had the name removed."
    )
    lifted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when the suppression was lifted; the row stays as the record.",
    )
    lift_reason = models.TextField(blank=True, help_text="Why it was lifted. Required to lift.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("creator__name",)
        constraints = (
            models.CheckConstraint(
                condition=models.Q(lifted_at__isnull=True) | ~models.Q(lift_reason=""),
                name="creator_suppression_lift_has_reason",
            ),
        )
        indexes = (models.Index(fields=("lifted_at",), name="creator_suppression_active_idx"),)

    def __str__(self) -> str:
        state = "lifted" if self.lifted_at else "active"
        return f"{self.creator_id}: suppression ({state})"

    @property
    def active(self) -> bool:
        return self.lifted_at is None


class WatchlistEntry(models.Model):
    """A creator the operator notes without indexing them right now (issue #71).

    Operator decision 2026-09-17: the watchlist records interest (note, added_at) and keeps the
    creator's site breadcrumbs in view; it is **not crawled** and does not keep pages active. Only
    an entry with `crawl` set is hunted, below every Auftrag creator. Staff-managed in the admin;
    removing an entry deletes no indexed data. `purpose` tells prospect research (seeded last,
    capped per day, `prospect_report`) from plain interest.
    """

    class Purpose(models.TextChoices):
        # Noted interest, e.g. a creator an operator keeps an eye on.
        INTEREST = "interest"
        # Prospect research before outreach (docs/prospect-research.md): how much of their
        # content circulates, reported by `prospect_report`. Seeded last and capped per day.
        PROSPECT = "prospect"

    creator = models.OneToOneField(
        Creator, on_delete=models.CASCADE, related_name="watchlist_entry"
    )
    note = models.TextField(blank=True, help_text="Why this creator is watched. No personal data.")
    crawl = models.BooleanField(
        default=False,
        help_text="Seed and index this creator (below Auftrag creators). Off: noted only.",
    )
    purpose = models.CharField(
        max_length=16,
        choices=Purpose,
        default=Purpose.INTEREST,
        help_text="Prospect: research before outreach (lowest seed priority, daily seed cap). "
        "Studio names and aliases go into the creator's aliases.",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("creator__name",)
        verbose_name_plural = "watchlist entries"

    def __str__(self) -> str:
        return str(self.creator)


class DoNotContactRecord(models.Model):
    """Someone told us not to contact them again (issue #98, ADR 0047, docs/do-not-contact.md).

    One row per **identifier**, not per person: a "no" given by letter blocks the postal address,
    and the same person's email is blocked only once it too is recorded. One person can therefore
    have several rows, and that is the intended shape — the record can only recognise what somebody
    hands it.

    **What is stored is a keyed hash** (`catalog.do_not_contact.digest`, HMAC-SHA256 over
    `kind:normalised identifier` with `DO_NOT_CONTACT_HMAC_KEY`), never the identifier. The row
    answers one closed question, asked with an identifier already in hand: *may we write to this
    address?* It cannot produce an address, a name or a list of people, and it is not a prospect
    file: no findings, no counts, no site ever goes next to it.

    **Permanent.** The whole point is that the objection outlives the campaign, the prospect row,
    the customer relationship and this year's database. It is not deletable in the admin, index
    suppression does not remove it, and no customer-data deletion may touch it. `creator` is
    `SET_NULL` for the same reason: a prospect row may go, the "no" stays.

    **Not index suppression.** `CreatorSuppression` answers "stop indexing me" (ADR 0038). Neither
    marker implies the other; both screens point at the other one.
    """

    identifier_kind = models.CharField(
        max_length=16,
        choices=do_not_contact.KINDS,
        help_text="What kind of identifier was recorded. It is part of the hash, so a record of "
        "one kind never blocks another.",
    )
    identifier_hash = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        help_text="HMAC-SHA256 of the normalised identifier. Not reversible, and useless without "
        "the server-side key.",
    )
    received_on = models.DateField(help_text="When the 'no' reached us (day precision).")
    channel = models.CharField(
        max_length=24, choices=do_not_contact.CHANNELS, help_text="How it reached us."
    )
    recorded_by = models.CharField(
        max_length=150, help_text="Who recorded it (username or 'operator')."
    )
    creator = models.ForeignKey(
        Creator,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="do_not_contact_records",
        help_text="The prospect row this 'no' came from, if there is one. Optional, and deleting "
        "that row leaves this one standing.",
    )
    note = models.TextField(
        blank=True,
        help_text="One line for staff, e.g. 'objection to the letter of <date>'. No names, no "
        "contact details, no quote from the message.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-received_on", "-id")
        verbose_name = "do-not-contact record"
        indexes = (models.Index(fields=("identifier_kind",), name="do_not_contact_kind_idx"),)

    def __str__(self) -> str:
        return f"do-not-contact ({self.identifier_kind}, {self.received_on})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Insert or update the row, journalling every **new** objection (ADR 0057).

        The journal hook sits here rather than in `catalog.services.record_no_contact` because this
        model has two writers: that function and the admin form, which sets `identifier_hash`
        itself. `save()` is the one thing both go through.

        The record is append-only, so a restore resurrects nothing — it **loses** every "no" given
        since the snapshot, which is the worse failure: we would write to someone who told us not
        to. What goes into the journal is exactly what is stored here anyway: the keyed digest (not
        reversible, useless without `DO_NOT_CONTACT_HMAC_KEY`), the kind, the date and the channel.
        The staff note and `recorded_by` stay out — free text never enters the journal — so a
        replayed row says in its note that it came back from the journal.
        """
        new = self._state.adding and self.pk is None
        super().save(*args, **kwargs)
        if new:
            deletion_journal.record(
                "do_not_contact_recorded",
                identifier_hash=self.identifier_hash,
                identifier_kind=self.identifier_kind,
                received_on=self.received_on,
                channel=self.channel,
                creator=self.creator_id,
            )


class SafetyTerm(models.Model):
    """A text indicator of illegal content (see docs/safety.md). Matched against page text.

    Text is compared case-insensitively on word boundaries after collapsing punctuation to single
    spaces. Plain terms match as whole words; regex terms run against that normalized text.
    """

    class Category(models.TextChoices):
        MINOR = "minor"
        NON_CONSENT = "non_consent"
        OTHER = "other"

    class Action(models.TextChoices):
        # What each action means: catalog/safety_rules.py (operator decisions 2026-09-17).
        REVIEW = "review", "Review (no media until a human clears it)"
        AUTO_BLOCK = "auto_block", "Auto-block (no review step, no media, no links)"
        AUTO_BLOCK_AND_INCIDENT = (
            "auto_block_and_incident",
            "Auto-block and open a safety incident (CSAM suspect)",
        )
        CUSTOMER_RELEASE_QUEUE = (
            "customer_release_queue",
            "Block; staff may release pages crediting a customer creator",
        )

    term = models.CharField(max_length=255, unique=True)
    is_regex = models.BooleanField(
        default=False, help_text="Regular expression over lowercased, space-normalized text."
    )
    category = models.CharField(max_length=16, choices=Category)
    action = models.CharField(max_length=32, choices=Action)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("action", "category", "term")

    def __str__(self) -> str:
        return self.term

    def clean(self) -> None:
        if self.is_regex:
            try:
                re.compile(self.term)
            except re.error as exc:
                raise ValidationError({"term": f"Invalid regular expression: {exc}"}) from exc


class SafetyRuleSet(models.Model):
    """A snapshot of the enabled safety terms, named by its content-addressed version.

    `catalog.safety_rules.rule_version` names a rule set; the row keeps what that name stood for,
    so a decision recorded with a version (`Page.safety_rule_version`,
    `index.SafetyRuleDecision.rule_version`) can be audited after the terms were edited. Written
    the first time a decision uses the version, never edited.
    """

    version = models.CharField(max_length=32, unique=True)
    terms = models.JSONField(
        default=list, help_text="Enabled terms at that version: term, is_regex, category, action."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.version


class HostingProvider(models.Model):
    """The network operator an address belongs to, keyed by its autonomous system number.

    The key for cooperation history (`HosterContact`): "have we complained to this operator
    before, and what happened". A CDN in front of a site is a provider like any other, because a
    notice to it is a notice all the same (ADR 0027).
    """

    asn = models.PositiveBigIntegerField(unique=True)
    name = models.CharField(max_length=255, blank=True, help_text="AS name as registered.")
    country = models.CharField(max_length=2, blank=True, help_text="ISO 3166-1 alpha-2.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("asn",)

    def __str__(self) -> str:
        return f"AS{self.asn} {self.name}".strip()


class HostingRecord(models.Model):
    """Who hosts one hostname and whom to complain to (ADR 0027). Hostnames only, never URLs.

    Filled by `lookup_hosting` from DNS and RDAP registries — never from the host itself. Every
    source is recorded (`abuse_source_url`, `registrar_source_url`) so a notice can say where the
    contact came from. Jurisdiction is **derived** from `country` at read time
    (`catalog.hosting.jurisdiction`), so a change to the mapping needs no re-lookup.
    """

    class Status(models.TextChoices):
        COMPLETE = "complete"
        PARTIAL = "partial"
        FAILED = "failed"

    class AbuseSource(models.TextChoices):
        IP_NETWORK = "ip_network", "RDAP abuse entity of the IP network"
        REGISTRAR = "registrar", "RDAP abuse contact of the domain's registrar"

    hostname = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=16, choices=Status)
    errors = models.JSONField(
        default=list, blank=True, help_text="Machine-readable codes of what could not be found."
    )
    addresses = models.JSONField(default=list, blank=True, help_text="Resolved A/AAAA records.")
    address = models.GenericIPAddressField(
        null=True, blank=True, help_text="The address the registry lookups were made for."
    )
    asn = models.PositiveBigIntegerField(null=True, blank=True)
    as_name = models.CharField(max_length=255, blank=True)
    provider = models.ForeignKey(
        HostingProvider,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="hosting_records",
    )
    network_name = models.CharField(max_length=255, blank=True)
    network_handle = models.CharField(max_length=255, blank=True)
    organisation = models.CharField(
        max_length=255, blank=True, help_text="Registrant organisation of the IP network."
    )
    country = models.CharField(
        max_length=2, blank=True, help_text="ISO 3166-1 alpha-2 of the IP allocation."
    )
    cdn = models.CharField(max_length=32, blank=True, help_text="CDN/proxy code, e.g. cloudflare.")
    abuse_email = models.CharField(max_length=254, blank=True)
    abuse_source = models.CharField(max_length=16, choices=AbuseSource, blank=True)
    abuse_source_url = models.URLField(max_length=2000, blank=True)
    registrable_domain = models.CharField(max_length=255, blank=True)
    registrar_name = models.CharField(max_length=255, blank=True)
    registrar_iana_id = models.CharField(max_length=32, blank=True)
    registrar_abuse_email = models.CharField(max_length=254, blank=True)
    registrar_source_url = models.URLField(max_length=2000, blank=True)
    looked_up_at = models.DateTimeField(help_text="Last lookup attempt, successful or not.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("hostname",)

    def __str__(self) -> str:
        return self.hostname


class HosterContact(models.Model):
    """One notice sent to a provider and what came of it — the shape of cooperation history.

    `marceline.notices.services` writes them (issue #33, ADR 0035): one row per **real** notice,
    opened the moment the customer marks it sent (`outcome=awaiting`) and settled when they record
    what the provider did. A sandbox notice never gets one. Marceline transmitting a notice on a
    customer's behalf stays blocked on #46.

    The pages a notice concerns are linked from the other side (`index.Page.notices`: `index` may
    import `catalog`, not the reverse). `removed_observed_at` is stamped from those pages'
    availability checks by `marceline.hosting.observe_notice_removals`, never typed by hand, and
    is only as precise as the recheck interval (ADR 0027 amendment).
    """

    class Channel(models.TextChoices):
        EMAIL = "email"
        WEB_FORM = "web_form"
        OTHER = "other"

    class Outcome(models.TextChoices):
        AWAITING = "awaiting"
        REMOVED = "removed"
        FORWARDED = "forwarded"
        REFUSED = "refused"
        NO_RESPONSE = "no_response"

    provider = models.ForeignKey(HostingProvider, on_delete=models.PROTECT, related_name="contacts")
    hostname = models.CharField(max_length=255, blank=True, help_text="Host the notice concerned.")
    channel = models.CharField(max_length=16, choices=Channel)
    sent_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=Outcome, default=Outcome.AWAITING)
    removed_observed_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="First availability check after sending that found every linked page gone. "
        "Stamped once; as precise as the recheck interval (about a day).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-sent_at",)
        indexes = (models.Index(fields=("provider", "-sent_at"), name="hoster_contact_sent_idx"),)

    def __str__(self) -> str:
        return f"{self.provider_id} {self.channel} {self.sent_at:%Y-%m-%d} {self.outcome}"


def validate_role_address(value: str) -> None:
    if value and not is_role_address(value):
        raise ValidationError(
            "Only a professional role address (abuse@, dmca@, legal-notices@ …), never a named "
            "person's mailbox."
        )


def validate_languages(value: str) -> None:
    if parse_languages(value) is None:
        raise ValidationError('ISO 639-1 codes separated by commas, e.g. "en, de".')


class ProviderNexus(models.TextChoices):
    """Where a provider faces. A hint for a person, not a verdict."""

    EU_FACING = "eu_facing", "EU-facing"
    US_NEXUS = "us_nexus", "US nexus"
    OTHER = "other", "Other"
    UNKNOWN = "unknown", "Unknown"


class ProviderFunction(models.TextChoices):
    """What a provider does for the material in its role (ADR 0027 amendment 2026-09-17)."""

    ORIGIN_HOST = "origin_host", "Origin host"
    FILE_HOST = "file_host", "File host"
    USER_CONTENT_PLATFORM = "user_content_platform", "User-content platform"
    REVERSE_PROXY_CDN = "reverse_proxy_cdn", "Reverse-proxy CDN"
    STORING_CDN = "storing_cdn", "CDN storing the material (verified)"
    REGISTRAR = "registrar", "Registrar"
    SEARCH_ENGINE = "search_engine", "Search engine"
    PAYMENT = "payment", "Payment"
    ADVERTISER = "advertiser", "Advertiser"
    OTHER = "other", "Other"
    UNKNOWN = "unknown", "Unknown"


class DirectoryProvider(models.Model):
    """One organisation in the staff-maintained provider directory (ADR 0027 amendment 2026-09-17).

    Linked to what a lookup finds through `ProviderIdentifier` (an AS number, a registrar's IANA
    id, a hostname). Nothing here is filled automatically. No personal data: organisations and role
    addresses only.
    """

    name = models.CharField(max_length=255, unique=True)
    establishment_country = models.CharField(
        max_length=2,
        blank=True,
        help_text="ISO 3166-1 alpha-2 where the provider says it is established, if published.",
    )
    nexus = models.CharField(
        max_length=16,
        choices=ProviderNexus,
        blank=True,
        help_text="Blank: follows the establishment country, else the IP allocation country. "
        "A location hint, not a legal verdict.",
    )
    nexus_note = models.TextField(blank=True, help_text="Why this nexus, with a source.")
    function = models.CharField(
        max_length=32,
        choices=[choice for choice in ProviderFunction.choices if choice[0] != "storing_cdn"],
        blank=True,
        help_text="Default function where none is mechanical (a network without a detected CDN, "
        "a hostname-linked service). A detected CDN stays a reverse proxy; storing is a per-host "
        "override.",
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(function="storing_cdn"),
                name="directory_provider_never_storing_cdn",
            ),
        )

    def __str__(self) -> str:
        return self.name


class ProviderIdentifier(models.Model):
    """How a lookup result finds its directory entry: an AS number, a registrar, a hostname.

    A hostname matches itself and every name below it (`example.com` matches `cdn.example.com`).
    """

    class Kind(models.TextChoices):
        ASN = "asn", "AS number (network)"
        REGISTRAR_IANA_ID = "registrar_iana_id", "Registrar IANA id"
        HOSTNAME = "hostname", "Hostname (the service itself)"

    provider = models.ForeignKey(
        DirectoryProvider, on_delete=models.CASCADE, related_name="identifiers"
    )
    kind = models.CharField(max_length=24, choices=Kind)
    value = models.CharField(max_length=255)

    class Meta:
        ordering = ("kind", "value")
        constraints = (
            models.UniqueConstraint(fields=("kind", "value"), name="provider_identifier_unique"),
        )

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"

    def clean(self) -> None:
        value = self.value.strip().lower()
        if self.kind == self.Kind.ASN:
            value = value.removeprefix("as")
        if self.kind in (self.Kind.ASN, self.Kind.REGISTRAR_IANA_ID) and not value.isdigit():
            raise ValidationError({"value": "A number."})
        if self.kind == self.Kind.HOSTNAME:
            value = value.rstrip(".")
            if "." not in value or any(char in value for char in "/:@ ?#"):
                raise ValidationError({"value": "A hostname such as example.com, not a URL."})
        self.value = value


class ComplaintChannel(models.Model):
    """One version of one complaint channel of a directory provider.

    **Versioned:** a change never edits a row. It writes a new current version and stamps the old
    one `superseded_at` / `superseded_by` (`catalog.hosting.directory.supersede_channel`), so what
    a channel said when it was checked stays readable. `checked_at` is when a person last verified
    it at `source_url`; after `PROVIDER_CHANNEL_RECHECK_DAYS` it is due again.

    Role addresses only (`abuse@`, `dmca@`) — never a named abuse officer. `transfer_basis` is set
    by staff per recipient; DPF certification is checked per organisation, never inferred from a
    country.
    """

    class Kind(models.TextChoices):
        DSA_ART16 = "dsa_art16", "DSA Art. 16 notice mechanism"
        DMCA_512C = "dmca_512c", "DMCA §512(c) (hosting)"
        DMCA_512D = "dmca_512d", "DMCA §512(d) (information location)"
        PROVIDER_COPYRIGHT_POLICY = "provider_copyright_policy", "Provider copyright policy"
        PROVIDER_COPYRIGHT_POLICY_DMCA_STYLE = (
            "provider_copyright_policy_dmca_style",
            "Provider copyright policy, DMCA-style",
        )
        ABUSE_EMAIL = "abuse_email", "Abuse mailbox"
        WEB_FORM = "web_form", "Web form"
        FORWARDING_ONLY = "forwarding_only", "Forwarding only"
        NONE_KNOWN = "none_known", "None known"

    class TransferBasis(models.TextChoices):
        NOT_REQUIRED = "not_required", "Not required (no third-country transfer)"
        ADEQUACY = "adequacy", "Adequacy decision"
        DPF = "dpf", "EU-US Data Privacy Framework (recipient certified)"
        ART46 = "art46", "Art. 46 safeguards"
        ART49_LEGAL_CLAIM = "art49_legal_claim", "Art. 49(1)(e) legal claims"
        COUNSEL_REVIEW = "counsel_review", "Counsel review needed"

    provider = models.ForeignKey(
        DirectoryProvider, on_delete=models.PROTECT, related_name="channels"
    )
    kind = models.CharField(max_length=40, choices=Kind)
    url = models.URLField(max_length=2000, blank=True, help_text="Form or policy page.")
    email = models.EmailField(
        blank=True,
        validators=[validate_role_address],
        help_text="A role address only (abuse@, dmca@); never a named person.",
    )
    languages = models.CharField(
        max_length=64,
        blank=True,
        validators=[validate_languages],
        help_text='Accepted languages if published, ISO 639-1: "en, de". Blank: not published.',
    )
    forwards_to_site_operator = models.BooleanField(
        null=True,
        blank=True,
        help_text="Does the provider pass complaint details on? Blank: unknown.",
    )
    forwarding_source_url = models.URLField(max_length=2000, blank=True)
    publishes_to_transparency_db = models.BooleanField(
        null=True,
        blank=True,
        help_text="Does it publish notices to a transparency database (e.g. Lumen)? "
        "Blank: unknown.",
    )
    transparency_source_url = models.URLField(max_length=2000, blank=True)
    transfer_basis = models.CharField(
        max_length=24, choices=TransferBasis, default=TransferBasis.COUNSEL_REVIEW
    )
    transfer_note = models.TextField(
        blank=True, help_text="E.g. where the recipient's DPF certification was checked."
    )
    source_url = models.URLField(max_length=2000, help_text="Where this channel is published.")
    checked_at = models.DateTimeField(help_text="When a person last verified this channel.")
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    superseded_at = models.DateTimeField(null=True, blank=True, editable=False)
    superseded_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        ordering = ("provider", "kind", "-checked_at")
        indexes = (
            models.Index(
                fields=("provider", "kind"),
                condition=models.Q(superseded_at__isnull=True),
                name="complaint_channel_current_idx",
            ),
        )
        constraints = (
            models.CheckConstraint(
                condition=models.Q(kind="none_known") | ~models.Q(url="") | ~models.Q(email=""),
                name="complaint_channel_has_address",
            ),
            models.CheckConstraint(
                condition=models.Q(forwards_to_site_operator__isnull=True)
                | ~models.Q(forwarding_source_url=""),
                name="complaint_channel_forwarding_sourced",
            ),
            models.CheckConstraint(
                condition=models.Q(publishes_to_transparency_db__isnull=True)
                | ~models.Q(transparency_source_url=""),
                name="complaint_channel_transparency_sourced",
            ),
        )

    def __str__(self) -> str:
        return f"{self.provider} {self.kind} ({self.checked_at:%Y-%m-%d})"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.kind != self.Kind.NONE_KNOWN and not self.url and not self.email:
            errors["url"] = "A channel needs a URL or a role address (or kind 'none known')."
        if self.forwards_to_site_operator is not None and not self.forwarding_source_url:
            errors["forwarding_source_url"] = "Say where the forwarding practice is published."
        if self.publishes_to_transparency_db is not None and not self.transparency_source_url:
            errors["transparency_source_url"] = "Say where the publication practice is stated."
        if errors:
            raise ValidationError(errors)


class ProviderFunctionOverride(models.Model):
    """A person's decision about a provider's function for one hostname, with the reason.

    Beats every derivation, and is the only way to call a CDN `storing_cdn`: that a CDN stores the
    material is verified, never inferred.
    """

    class Slot(models.TextChoices):
        SERVICE = "service", "Service (the hostname's own operator)"
        NETWORK = "network", "Network (the AS serving the hostname)"
        REGISTRAR = "registrar", "Registrar of the hostname's domain"

    hostname = models.CharField(max_length=255)
    slot = models.CharField(max_length=16, choices=Slot)
    function = models.CharField(max_length=32, choices=ProviderFunction)
    note = models.TextField(help_text="Why: what was verified, and where.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("hostname", "slot")
        constraints = (
            models.UniqueConstraint(
                fields=("hostname", "slot"), name="provider_function_override_unique"
            ),
            models.CheckConstraint(
                condition=~models.Q(note=""), name="provider_function_override_has_note"
            ),
        )

    def __str__(self) -> str:
        return f"{self.hostname} {self.slot} -> {self.function}"

    def clean(self) -> None:
        self.hostname = self.hostname.strip().rstrip(".").lower()
        if "." not in self.hostname or any(char in self.hostname for char in "/:@ ?#"):
            raise ValidationError({"hostname": "A hostname such as cdn.example.com, not a URL."})


class HostRole(models.TextChoices):
    """Whose server a request goes to (ADR 0007 amendment 2026-09-17)."""

    ORIGIN = "origin", "Origin (the site's own servers)"
    CDN = "cdn", "CDN (a media host serving the site)"
    THIRD_PARTY = "third_party", "Third party (RDAP, search API, ...)"


class SiteHost(models.Model):
    """The role of one host a site's requests go to, and a CDN host's own daily budget.

    Rows are observed from media URLs (`catalog.request_accounting.observe_site_hosts`), seeded
    from the reviewed `SitePolicy.cdn_hosts`, or set by the operator in the admin; operator rows
    are never overwritten. A host without a row is classified by rule: the site's domain and its
    subdomains are origin, any other host is a CDN.
    """

    class Source(models.TextChoices):
        OBSERVED = "observed"
        POLICY = "policy"
        OPERATOR = "operator"

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="hosts")
    host = models.CharField(max_length=255)
    role = models.CharField(
        max_length=16,
        choices=[
            (HostRole.ORIGIN.value, HostRole.ORIGIN.label),
            (HostRole.CDN.value, HostRole.CDN.label),
        ],
    )
    source = models.CharField(max_length=16, choices=Source, default=Source.OBSERVED)
    daily_request_budget = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="CDN hosts only: requests per rolling 24 h across every site using this host. "
        "Blank: the default CDN budget. With several sites, the lowest value set applies.",
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("site__domain", "host")
        constraints = (models.UniqueConstraint(fields=("site", "host"), name="uniq_site_host"),)
        indexes = (models.Index(fields=("host",), name="sitehost_host_idx"),)

    def __str__(self) -> str:
        return f"{self.host} ({self.role})"


class RequestCountBase(models.Model):
    """Outgoing HTTP requests, aggregated: one row per period and combination of dimensions.

    `kind` is what the request was for (`page`, `robots`, `media`, `probe`, `rdap`, ...) and
    keys the budgets; `request_type` is the same, except `retry` or `redirect_hop` for a repeated
    attempt or a followed redirect. `outcome`: `2xx`..`5xx`, `error` (no response), `refused`
    (refused before sending, not a request). Names are stable: ADR 0030 reads these counters.
    """

    site = models.ForeignKey(
        Site, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    host = models.CharField(max_length=255)
    role = models.CharField(max_length=16, choices=HostRole)
    kind = models.CharField(max_length=32)
    request_type = models.CharField(max_length=32)
    outcome = models.CharField(max_length=16)
    requests = models.PositiveBigIntegerField(default=0)
    bytes = models.PositiveBigIntegerField(default=0)

    class Meta:
        abstract = True


class RequestCount(RequestCountBase):
    """Requests per UTC hour, kept `HOURLY_RETENTION` long and then rolled up per day."""

    hour = models.DateTimeField()

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("hour", "site", "host", "role", "kind", "request_type", "outcome"),
                name="uniq_request_count_hour",
                nulls_distinct=False,
            ),
        )
        indexes = (
            models.Index(fields=("site", "hour"), name="request_count_site_hour_idx"),
            models.Index(fields=("host", "hour"), name="request_count_host_hour_idx"),
            models.Index(fields=("hour",), name="request_count_hour_idx"),
        )

    def __str__(self) -> str:
        return f"{self.hour:%Y-%m-%d %H}h {self.host} {self.request_type} {self.outcome}"


class RequestCountDaily(RequestCountBase):
    """Requests per UTC day, rolled up from hourly rows past their retention; kept forever."""

    day = models.DateField()

    class Meta:
        verbose_name_plural = "request counts daily"
        constraints = (
            models.UniqueConstraint(
                fields=("day", "site", "host", "role", "kind", "request_type", "outcome"),
                name="uniq_request_count_day",
                nulls_distinct=False,
            ),
        )
        indexes = (models.Index(fields=("site", "day"), name="request_count_site_day_idx"),)

    def __str__(self) -> str:
        return f"{self.day} {self.host} {self.request_type} {self.outcome}"


class MediaUrlShape(models.Model):
    """What one site's media URL *shape* has been answering, and whether it is suppressed.

    ADR 0041 (issue #82): a site whose theme advertises a media URL it never generated answers 404
    for every page we index, and each 404 costs a request and — site17.example's error page was
    246 kB — real bandwidth. One row per (site, shape), where a shape is `core.url_shapes.
    url_shape`: the host plus the path with its identifier-looking segments starred. The same
    shaping the capture refusals of ADR 0016 store, so the two always mean the same thing.

    Only a settled "this is not here" moves this row: HTTP 404/410 on a URL that is not a tokenized
    expiring link (`gone`), and any 2xx (`ok`). A 5xx, a 429, a timeout, a 403, a robots.txt or
    budget refusal and an expired token leave it untouched — none of them says the URL is wrong,
    and suppressing on one would silently stop us fingerprinting a working site.

    `gone_recent`/`ok_recent` are a decayed window (both halved once their sum passes
    `MEDIA_SHAPE_WINDOW`), not a lifetime total: the rule asks what the shape is answering *now*,
    and two integers survive two workers updating the same row.
    """

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="media_shapes")
    shape = models.CharField(max_length=500)
    gone_streak = models.PositiveIntegerField(
        default=0, help_text="Consecutive 404/410 answers; any 2xx resets it."
    )
    gone_recent = models.PositiveIntegerField(default=0)
    ok_recent = models.PositiveIntegerField(default=0)
    gone_total = models.PositiveBigIntegerField(default=0)
    ok_total = models.PositiveBigIntegerField(default=0)
    last_gone_at = models.DateTimeField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    suppressed_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set: media of this shape is postponed until then (open), or the window has "
        "passed and the next row claimed is the probe (half-open). Blank: not suppressed.",
    )
    suppressed_at = models.DateTimeField(null=True, blank=True)
    backoff_step = models.PositiveSmallIntegerField(
        default=0, help_text="How often it re-tripped without a success in between."
    )
    trips = models.PositiveIntegerField(default=0)
    probe_started_at = models.DateTimeField(
        null=True, blank=True, help_text="When the half-open probe was handed out."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("site__domain", "shape")
        verbose_name = "media URL shape"
        constraints = (
            models.UniqueConstraint(fields=("site", "shape"), name="uniq_media_url_shape"),
        )
        indexes = (
            models.Index(fields=("site", "suppressed_until"), name="media_shape_site_supp_idx"),
        )

    def __str__(self) -> str:
        return self.shape


class CatalogueSizeMethod(models.TextChoices):
    """How a site's catalogue total was obtained. Not equally good, hence a closed list.

    "The site states 66,758 videos" and "we counted the entries of its own sitemap" are both
    denominators, but only one of them was counted by us. The method is stored beside the figure so
    nobody has to reconstruct it from a note later, and so an automatic capture can never quietly
    replace a better figure with a worse one (`catalog.catalogue_size.METHOD_RANK`).
    """

    LISTING_PAGES = (
        "listing_pages_x_page_size",
        "Last listing page number x the adapter's page size (an estimate)",
    )
    STATED_ON_SITE = (
        "stated_on_site",
        "The site states the number itself, on a page we fetch anyway",
    )
    SITEMAP_COUNT = "sitemap_count", "Counted from the site's own sitemap"
    MANUAL = "manual", "Entered by a person, from a recorded reconnaissance note"


class SiteCatalogueSize(models.Model):
    """How large a site's own catalogue is, and how much of it we hold (issue #107, ADR 0049).

    The denominator of our share of a site's catalogue. On 2026-09-18 that share could be
    computed for only two of the enabled sites, because nothing recorded the total (tripwire T3).
    This row is that record: the site's own
    stated or derivable total, where it came from, when it was last seen, and a note saying how
    much it is worth.

    **It costs no request.** A figure only ever arrives from a page the crawler was going to fetch
    anyway — a site that prints "N videos" in its own page furniture (`SiteAdapter.catalogue_total`,
    `crawler.catalogue_totals`) — or from a person typing in what a recorded reconnaissance note
    already holds (`manage.py catalogue_size --record`). No sitemap fetch, no probe, no new request
    type; a site that states nothing simply has no row, and unknown is reported as unknown.

    `pages_indexed` / `pages_active` are the **numerator**, and they are written from the `index`
    app (`index.catalogue_share`), which is the only side that may count pages — `catalog` does not
    import `index`. They are a stamped observation, not a live count: `counted_at` says when. Both
    are kept because targeted indexing (ADR 0029) makes them very different numbers, and the honest
    statement needs both: 58 % of everything ever indexed is already deactivated and scrubbed.
    """

    site = models.OneToOneField(Site, on_delete=models.CASCADE, related_name="catalogue_size")
    total = models.PositiveIntegerField(
        help_text="How many videos the site publishes, by its own account. The denominator."
    )
    method = models.CharField(max_length=32, choices=CatalogueSizeMethod)
    source_path = models.CharField(
        max_length=200,
        blank=True,
        help_text="Where the figure was read, as a path shape (`/artist/*`, `/sitemap.xml`) or "
        "the name of a recorded note. Never a full URL, never an identifier.",
    )
    note = models.TextField(
        blank=True,
        help_text="How much this figure is worth: what it counts, what it leaves out, what it was "
        "cross-checked against. One or two lines.",
    )
    recorded_by = models.CharField(
        max_length=150,
        blank=True,
        help_text="Who or what recorded it: a username, `crawler`, or a command's name.",
    )
    observed_at = models.DateTimeField(help_text="When this figure was last seen to be current.")
    first_recorded_at = models.DateTimeField(auto_now_add=True)
    pages_indexed = models.PositiveIntegerField(
        default=0,
        help_text="Pages of this site ever indexed. Written by `index.catalogue_share`.",
    )
    pages_active = models.PositiveIntegerField(
        default=0, help_text="Of those, still active under targeted indexing (ADR 0029)."
    )
    counted_at = models.DateTimeField(
        null=True, blank=True, help_text="When the two counts above were measured. Empty = never."
    )
    notified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the operator was last alerted that this site is over the monitoring "
        "threshold. Cleared when the share falls back under it, so one crossing is one mail.",
    )

    class Meta:
        ordering = ("site__domain",)
        verbose_name = "site catalogue size"
        verbose_name_plural = "site catalogue sizes"
        constraints = (
            models.CheckConstraint(
                condition=models.Q(total__gt=0), name="catalogue_size_total_positive"
            ),
        )

    def __str__(self) -> str:
        return f"{self.site_id}: {self.total:,} ({self.method})"

    @property
    def share_indexed(self) -> float | None:
        """Percent of the site's catalogue ever indexed, or None while no total is known."""
        return None if not self.total else 100.0 * self.pages_indexed / self.total

    @property
    def share_active(self) -> float | None:
        """Percent of the site's catalogue held *active* today (ADR 0029)."""
        return None if not self.total else 100.0 * self.pages_active / self.total


class SiteObjection(models.Model):
    """A site operator told us to stop, and what we did about it (issue #108, ADR 0050).

    A performer who is not our customer has a real route: index suppression, ADR 0038,
    `docs/suppression.md`, a database trigger. A **site** had none — the answer was
    `Site.enabled = False` by hand, and nothing recorded who asked, when, on what basis, what was
    decided or by whom (`docs/legal/crawl-position.md`, tripwire T6). This row is the record, and
    `catalog.site_objections` is the one path that turns it into a switch.

    **What may go in here:** the site, dates, a channel and a kind as codes, the decision and who
    took it, and a short staff note about the *substance*. **Never** the sender's name, address,
    signature block, telephone number or a copy of their letter — that correspondence stays in the
    operator's mailbox, exactly as for `CreatorSuppression`. The note is what whoever handles the
    next one reads; it is not a case file.

    Deliberately **not** a switch of its own: a decision is applied through the switches that
    already exist (`Site.enabled`, `Site.paused_until`, the origin media budget), so there is one
    place that stops a crawl and no second, forgotten one. Procedure:
    `docs/runbooks/site-objection.md`.
    """

    class Kind(models.TextChoices):
        OPERATOR_REQUEST = "operator_request", "The site operator asked us directly"
        LEGAL_LETTER = "legal_letter", "A lawyer's letter"
        PROVIDER_COMPLAINT = "provider_complaint", "An abuse complaint our provider forwarded"
        POLICY_CHANGE = "policy_change", "A robots.txt or terms change we noticed ourselves"

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        POST = "post", "Postal letter"
        PROVIDER = "provider", "Relayed by our hosting provider"
        OBSERVED = "observed", "Observed by us; nobody wrote to us"
        OTHER = "other", "Other — see the note"

    class Decision(models.TextChoices):
        PENDING = "pending", "Not decided yet"
        PAUSED = "paused", "Paused pending review (reversible, applied at once)"
        DISABLED = "disabled", "Site disabled: no page of it is crawled again"
        MEDIA_STOPPED = "media_stopped", "Media fetching stopped; page text still indexed"
        DECLINED = "declined", "Declined, with the reason recorded"

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="objections")
    received_on = models.DateField(help_text="When it reached us (day precision).")
    kind = models.CharField(max_length=24, choices=Kind, default=Kind.OPERATOR_REQUEST)
    channel = models.CharField(max_length=16, choices=Channel, default=Channel.EMAIL)
    note = models.TextField(
        blank=True,
        help_text="What was asked, in one or two lines. No personal data of the sender beyond "
        "what is needed to act: no name, address, telephone number, signature block or quoted "
        "letter — that stays in the mailbox.",
    )
    decision = models.CharField(max_length=16, choices=Decision, default=Decision.PENDING)
    decision_reason = models.TextField(
        blank=True, help_text="Why this decision was taken. Required to decline."
    )
    decided_by = models.CharField(
        max_length=150, blank=True, help_text="Who decided (username or 'operator')."
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the decision was last applied to the site's switches.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-received_on", "-created_at")
        verbose_name = "site objection"
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(decision="declined") | ~models.Q(decision_reason=""),
                name="site_objection_declined_has_reason",
            ),
        )
        indexes = (models.Index(fields=("decision",), name="site_objection_decision_idx"),)

    def __str__(self) -> str:
        return f"{self.site_id}: {self.kind} {self.received_on} ({self.decision})"


class SiteRobotsPolicy(models.Model):
    """What one host's robots.txt reserves, and against whom (tripwire T2, issue #105).

    One row per (site, host): a site's own server and each media host have their own robots.txt, and
    only the host that answered can be said to reserve anything. Written by the crawler from the
    robots.txt it already fetches — never by a request of its own — and holding only what
    `core.robots.RobotsFacts` holds: user-agent tokens, booleans, a content hash. **No path, no line
    of the file, no page text.**

    Three states matter, and they are deliberately not one field:

    * `names_us` — a group naming our own product token shuts us out of the whole site, or the
      response carried a `TDM-Reservation` / `X-Robots-Tag: noai` header. Unambiguous: the site
      reserves itself against us. With `ROBOTS_RESERVATION_PAUSE_ENABLED` the site is paused for
      operator review (`pause_reason` `tdm_reservation`).
    * `names_wildcard` — the `*` group shuts out the whole site. Already obeyed: `parse_robots`
      selects that group for us and every URL is skipped before a request. Recorded, never paused.
    * `third_party_ai` — the reservation is addressed to other companies' AI crawler tokens and to
      neither `*` nor us. It is recorded, reported and alerted on once, and never acted on by
      itself: the operator decides per site.
    """

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="robots_policies")
    host = models.CharField(max_length=255, help_text="The host whose robots.txt this describes.")
    content_hash = models.CharField(
        max_length=64, help_text="SHA-256 of the body; what tells a change from a re-read."
    )
    agents = models.JSONField(
        default=list, blank=True, help_text="User-agent tokens the file addresses."
    )
    full_disallow_agents = models.JSONField(
        default=list, blank=True, help_text="Tokens the file shuts out of the whole host."
    )
    ai_tokens = models.JSONField(
        default=list,
        blank=True,
        help_text="Known AI/TDM crawler tokens shut out of the whole host, other than ours.",
    )
    reservation_headers = models.JSONField(
        default=list,
        blank=True,
        help_text="Response headers that carried a reservation (TDM-Reservation, X-Robots-Tag).",
    )
    names_us = models.BooleanField(
        default=False, help_text="A full-host disallow names our product token, or a header did."
    )
    names_wildcard = models.BooleanField(
        default=False, help_text="A full-host disallow names `*` — already obeyed by the crawler."
    )
    third_party_ai = models.BooleanField(
        default=False, help_text="A full-host disallow names other AI/TDM crawlers, not us."
    )
    first_seen_at = models.DateTimeField(auto_now_add=True)
    changed_at = models.DateTimeField(help_text="When the content hash last changed.")
    checked_at = models.DateTimeField(help_text="When the crawler last read this robots.txt.")
    notified_at = models.DateTimeField(
        null=True, blank=True, help_text="When the operator was last alerted about this row."
    )
    notified_hash = models.CharField(
        max_length=64, blank=True, help_text="The content hash the last alert was about."
    )

    class Meta:
        ordering = ("site__domain", "host")
        verbose_name = "site robots policy"
        verbose_name_plural = "site robots policies"
        constraints = (
            models.UniqueConstraint(fields=("site", "host"), name="uniq_site_robots_policy"),
        )

    def __str__(self) -> str:
        return self.host

    @property
    def reserved(self) -> bool:
        """Does this host declare a machine-readable reservation of any kind?"""
        return self.names_us or self.names_wildcard or self.third_party_ai


class SitePolicyChange(models.Model):
    """One change to a policy-bearing field of a `Site`: what changed, from what, by whom, why.

    Tripwire T4 (issue #106). On 2026-09-18 `Site.capture_play_click` went from false to true on one
    site and nothing in the system recorded who did it or why — it was a deliberate operator
    decision, visible only on a GitHub issue, and the change was noticed at all only because a
    measurement was repeated forty minutes later and disagreed with itself.

    **Written by a database trigger**, not by Python (`catalog/migrations/0024_site_policy_audit`):
    the flip that started this was a queryset `.update()`, which fires no model signal, and the
    admin, a management command, a shell `.save()` and raw SQL all have to be covered by the same
    record. `catalog.policy_audit.policy_actor` names the actor and the reason for the trigger
    through two transaction-local Postgres settings; without it the actor reads `database`.

    **Append-only**: UPDATE and DELETE are refused by a trigger, like the rights and notice logs.
    `age_confirmation` is recorded as its *shape* (`empty` / `approval, N cookie(s)`) — never a
    cookie name or value.
    """

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="policy_changes")
    field = models.CharField(max_length=64)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    actor = models.CharField(
        max_length=200,
        help_text="`admin:<user>`, a management command's name, or `database` when a writer set no "
        "actor (a bare queryset update, a shell save, raw SQL).",
    )
    reason = models.TextField(
        blank=True, help_text="Why. Required in the admin for the legally weighty switches."
    )
    changed_at = models.DateTimeField()

    class Meta:
        ordering = ("-changed_at", "field")
        verbose_name = "site policy change"
        indexes = (
            models.Index(fields=("-changed_at",), name="policy_change_when_idx"),
            models.Index(fields=("site", "field"), name="policy_change_site_field_idx"),
        )

    def __str__(self) -> str:
        return f"{self.site_id} {self.field}: {self.old_value} → {self.new_value}"
