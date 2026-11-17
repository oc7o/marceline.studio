"""What the crawler found: pages on piracy sites, their media URLs and PDQ frame hashes.

Media files themselves are never stored — only URLs, content hashes and fingerprints.
"""

from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models.functions import Length
from django.db.models.lookups import Exact

from catalog.models import Creator, HosterContact, Site
from core.public_ids import public_id_field

PDQ_HASH_BYTES = 32
# The frame embedding's sign sketch (ADR 0037). Declared here rather than imported, because
# `index` may not import `matching` (module boundaries) — exactly like PDQ_HASH_BYTES above.
# `matching.embedding.SKETCH_BYTES` is the definition; a test keeps the two equal.
EMBEDDING_SKETCH_BYTES = 64


class Page(models.Model):
    """One uploaded video on a piracy site."""

    class Availability(models.TextChoices):
        PLAYABLE = "playable"
        REMOVED = "removed"
        UNKNOWN = "unknown"

    class SafetyStatus(models.TextChoices):
        CLEAR = "clear"
        REVIEW = "review"
        BLOCKED = "blocked"

    class SafetyRelease(models.TextChoices):
        # Customer release queue (catalog.safety_rules, operator decision 2026-09-17): a page
        # blocked only by `customer_release_queue` terms that credits a customer's rights holder.
        NONE = "", "—"
        QUEUED = "queued", "Queued for staff (customer credited)"
        RELEASED = "released", "Released for fingerprinting by staff"
        KEPT_BLOCKED = "kept_blocked", "Kept blocked by staff"
        REVOKED = "revoked", "Release revoked (another safety term matched)"

    class InactiveReason(models.TextChoices):
        # No target creator link, no match/leak/cluster link to a target original, not found
        # through a target's seed (marceline.targets).
        UNTARGETED = "untargeted"

    availability = models.CharField(
        max_length=16,
        choices=Availability,
        default=Availability.UNKNOWN,
        db_default=Availability.UNKNOWN,
        db_index=True,
    )
    availability_checked_at = models.DateTimeField(null=True, blank=True)
    safety_status = models.CharField(
        max_length=16,
        choices=SafetyStatus,
        default=SafetyStatus.CLEAR,
        db_default=SafetyStatus.CLEAR,
        db_index=True,
        help_text="Text-based content-safety flag. Blocked pages: follow docs/safety.md.",
    )
    safety_matches = models.JSONField(
        default=list,
        db_default=[],
        blank=True,
        help_text="Matched safety terms: term, category, action.",
    )
    safety_flagged_at = models.DateTimeField(null=True, blank=True)
    safety_reviewed_at = models.DateTimeField(
        null=True, blank=True, help_text="Set when a human decided the status."
    )
    safety_note = models.TextField(
        blank=True, help_text="Reviewer's reason; required to change a blocked page."
    )
    safety_score = models.FloatField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "How clearly safe the page's own text is, 0.0-1.0 (ADR 0018). Media is only fetched "
            "at or above the configured threshold. NULL: never scored (no detail parse yet)."
        ),
    )
    safety_score_reason = models.CharField(
        max_length=64,
        blank=True,
        help_text="Deciding reason for the score, e.g. 'age_pattern' or 'clearly_safe'.",
    )
    safety_score_signals = models.JSONField(
        default=list,
        db_default=[],
        blank=True,
        help_text="Every scoring signal that fired, for review.",
    )
    safety_scored_at = models.DateTimeField(null=True, blank=True)
    safety_rule_version = models.CharField(
        max_length=32,
        blank=True,
        help_text="Version of the safety rule set (catalog.SafetyRuleSet) behind `safety_matches`.",
    )
    safety_release = models.CharField(
        max_length=16,
        choices=SafetyRelease,
        blank=True,
        default=SafetyRelease.NONE,
        db_default=SafetyRelease.NONE,
        db_index=True,
        help_text=(
            "Customer release queue state (docs/safety.md). Only `released` lets a page blocked "
            "by a customer_release_queue term become clear; any other safety hit revokes it."
        ),
    )
    safety_release_creator = models.ForeignKey(
        Creator,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="The customer's rights holder the page credits (why it was queued).",
    )
    safety_release_at = models.DateTimeField(null=True, blank=True)
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="pages")
    url = models.URLField(max_length=2000, unique=True)
    #: What the customer API calls this page (a finding's `page_id`); the primary key never leaves
    #: (ADR 0033). The database fills it too, so a crawler that predates the column still inserts.
    public_id = public_id_field("page")
    title = models.TextField(blank=True)
    tags = models.JSONField(default=list, db_default=[], blank=True)
    creators = models.ManyToManyField(Creator, blank=True, related_name="pages")
    notices = models.ManyToManyField(
        HosterContact,
        blank=True,
        related_name="pages",
        help_text="Takedown notices that named this page; their removal is observed here.",
    )
    uploaded_at = models.DateTimeField(null=True, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    raw_extraction = models.JSONField(
        default=dict, db_default={}, blank=True, help_text="What the LLM or parser returned."
    )
    # ADR 0042, issue #22: who produced the fields above. `llm_repair` means a model was asked to
    # read them off the page's text because the site's adapter was recorded broken — a guess about
    # the page, never the page's own wording, and never evidence of anything. It is a column and
    # not a key in `raw_extraction` on purpose: a later cleanup has to be able to find every such
    # row with one query, and `raw_extraction` is rewritten by scrubbing and by a block.
    extraction_source = models.CharField(
        max_length=16,
        choices=(("adapter", "Adapter"), ("llm_repair", "LLM repair")),
        default="adapter",
        db_default="adapter",
        db_index=True,
        help_text="Who produced this page's fields: the site's adapter, or a repairing model.",
    )
    # Targeted indexing (issue #71): a page that belongs to no target creator is deactivated,
    # never deleted. Matching, clustering, duplicates, common frames and availability rechecks
    # only look at active pages. `marceline.targets` owns the rule; new pages start active.
    active = models.BooleanField(
        default=True,
        db_default=True,
        db_index=True,
        help_text="Considered by matching and rechecks. Inactive pages are kept, not queried.",
    )
    inactive_reason = models.CharField(
        max_length=32,
        choices=InactiveReason,
        blank=True,
        help_text="Why the page is inactive; empty while active.",
    )
    active_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Last (de)activation. Incremental matching rescans pages reactivated since.",
    )
    # Retention (ADR 0029 amendment 2026-09-17, index.retention): the clock a deactivated page's
    # finite retention runs from. Set when `marceline.targets.apply_activity` deactivates a page
    # (alongside scrubbing title/tags/performer credits — never the creator links, which the
    # `linked` reactivation rule reads), cleared the moment it
    # reactivates. `page_retention` deletes the row outright once it has run past
    # `index.retention.PAGE_RETENTION_MONTHS`. NULL while the page is active or was never
    # deactivated by this rule.
    deactivated_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=models.Q(active=True, inactive_reason="")
                | models.Q(active=False) & ~models.Q(inactive_reason=""),
                name="page_inactive_has_reason",
            ),
        )
        indexes = (
            models.Index(fields=("site", "-last_seen"), name="page_site_last_seen_idx"),
            # The media claim's page side (matching.media.claim_media): a site's pages whose media
            # may be fetched at all. Small next to the site's page list once pages go inactive.
            models.Index(
                fields=("site", "id"),
                name="page_site_claimable_idx",
                condition=models.Q(active=True, safety_status="clear"),
            ),
            GinIndex(OpClass("title", name="gin_trgm_ops"), name="page_title_trgm_idx"),
        )

    def __str__(self) -> str:
        return self.title or self.url


class SafetyRuleDecision(models.Model):
    """Append-only audit log of rule-driven and human safety decisions on a page (docs/safety.md).

    Every auto-block (with the matched rule and the rule set version), every customer release
    queue step and every staff reversal is one row. Only our own rule terms are recorded — never
    page content: no title, tags, text or media.
    """

    class Kind(models.TextChoices):
        AUTO_BLOCK = "auto_block"
        AUTO_BLOCK_AND_INCIDENT = "auto_block_and_incident"
        # Blocked by a customer_release_queue term (whether or not it is queued).
        RELEASE_HOLD = "release_hold"
        QUEUED = "queued"
        DEQUEUED = "dequeued"
        RELEASED = "released"
        KEPT_BLOCKED = "kept_blocked"
        RELEASE_REVOKED = "release_revoked"
        # A human changed the safety status (admin or review API), e.g. reversing an auto-block.
        STAFF_STATUS = "staff_status"

    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="safety_decisions")
    kind = models.CharField(max_length=32, choices=Kind, db_index=True)
    status = models.CharField(
        max_length=16, blank=True, help_text="The page's safety status after this decision."
    )
    terms = models.JSONField(
        default=list,
        db_default=[],
        blank=True,
        help_text="Matched rule terms: term, category, action.",
    )
    rule_version = models.CharField(max_length=32, blank=True)
    creator = models.ForeignKey(
        Creator,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Customer creator the page credits (release queue decisions).",
    )
    decided_by = models.CharField(
        max_length=150, blank=True, help_text="Username; blank for an automatic decision."
    )
    note = models.TextField(blank=True, help_text="Staff reason. Never page content.")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    def __str__(self) -> str:
        return f"{self.kind} page={self.page_id}"


class SafetyIncident(models.Model):
    """One content-safety case: what was flagged, whether it was reported, and when it may go.

    A *case*, not a page: the same content found again — on a re-crawl, mirrored at another URL,
    or re-flagged after a term-list change — joins the incident it already belongs to instead of
    opening a new one (`index/incidents.py`). That is what keeps an automated feed from flooding a
    hotline with the same finding.

    **Never any page content.** URL, timestamps, the matched category and term, and reporting
    metadata only — no title, no tags, no extracted text, nothing about the media (docs/safety.md).
    The record exists to prove to a hotline or a prosecutor what was found and when.

    Retention (operator decision 2026-09-16): kept while the case is open, then twelve months after
    it closes, then deleted — `safety_retention`.
    """

    class Status(models.TextChoices):
        OPEN = "open"
        REPORTED = "reported"
        CLOSED = "closed"
        FALSE_POSITIVE = "false_positive"

    status = models.CharField(
        max_length=16,
        choices=Status,
        default=Status.OPEN,
        db_default=Status.OPEN,
        db_index=True,
    )
    category = models.CharField(
        max_length=32,
        blank=True,
        help_text="Matched safety category (catalog.SafetyTerm): minor, non_consent, other.",
    )
    term = models.CharField(
        max_length=255, blank=True, help_text="The deciding matched term. Never page content."
    )
    terms = models.JSONField(
        default=list,
        db_default=[],
        blank=True,
        help_text="Every {category, term} this case was flagged for, including later term hits.",
    )
    detected_at = models.DateTimeField(db_index=True, help_text="When the flag was first raised.")
    last_seen_at = models.DateTimeField(help_text="When the content was last seen, e.g. a recrawl.")
    reported_at = models.DateTimeField(null=True, blank=True)
    reported_via = models.CharField(
        max_length=255, blank=True, help_text="Channel, e.g. the hotline's web form."
    )
    reference = models.CharField(
        max_length=128,
        blank=True,
        help_text="The hotline's reference number. Blank means none was issued.",
    )
    report_category = models.CharField(
        max_length=255, blank=True, help_text="Category chosen in the hotline's form, verbatim."
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.CharField(
        max_length=150, blank=True, help_text="Who decided (username or 'operator')."
    )
    note = models.TextField(blank=True, help_text="Free text about the case. Never page content.")
    cluster_ref = models.PositiveIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "matching.Cluster id the pages of this case belong to, deliberately a plain id and "
            "not a foreign key: index must not depend on matching (module boundaries)."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(status="reported") | models.Q(reported_at__isnull=False),
                name="incident_reported_has_date",
            ),
            models.CheckConstraint(
                condition=~models.Q(status__in=("closed", "false_positive"))
                | models.Q(closed_at__isnull=False),
                name="incident_closed_has_date",
            ),
        )
        indexes = (models.Index(fields=("status", "closed_at"), name="incident_retention_idx"),)

    def __str__(self) -> str:
        return f"incident {self.pk} ({self.status})"


class SafetyIncidentPage(models.Model):
    """One page (one URL) belonging to a case, and why it was attached to it.

    The URL is copied so the record survives the page row: a page may be deleted, the incident is
    what a hotline follow-up refers to. Nothing else of the page is copied.
    """

    class Evidence(models.TextChoices):
        # The page the case was opened for.
        ORIGIN = "origin"
        # (a) The same URL seen again on a re-crawl.
        SAME_URL = "same_url"
        # (c) The same page re-flagged after a term-list change.
        TERM_CHANGE = "term_change"
        # (b) The same content elsewhere: a shared mirror poster/frame key, identical media bytes,
        # or the same video cluster.
        MIRROR = "mirror"
        SHA256 = "sha256"
        CLUSTER = "cluster"
        # (b) The same material, recognised by a PDQ hash retained on an existing case
        # (`SafetyIncidentHash`) — the only same-content evidence a page blocked on first sight
        # can still produce (ADR 0011 amendment 2026-09-17).
        PDQ = "pdq"
        # A human attached this page.
        MANUAL = "manual"

    incident = models.ForeignKey(
        SafetyIncident, on_delete=models.CASCADE, related_name="incident_pages"
    )
    page = models.ForeignKey(
        Page,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="safety_incidents",
        help_text="Null once the page row is gone; the URL below stays as the record.",
    )
    url = models.URLField(max_length=2000)
    site_domain = models.CharField(max_length=255, blank=True)
    evidence = models.CharField(
        max_length=16, choices=Evidence, help_text="Why this page belongs to this case."
    )
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("incident", "url"), name="uniq_incident_page_url"),
        )

    def __str__(self) -> str:
        return f"{self.incident_id}: {self.url} ({self.evidence})"


class SafetyIncidentHash(models.Model):
    """A PDQ hash of one picture of a blocked page, kept on its case so the same material is
    recognised instead of reported twice (ADR 0011 amendment 2026-09-17).

    **One purpose only.** These hashes decide whether a newly blocked page is the same material as
    an existing case (`index/incidents.py`). They are not evidence that anything was copied, and
    **no matching query may read this table** — that is why they live here and not in `FrameHash`,
    which every matcher loads.

    **Nothing is ever fetched for them.** A row only ever holds a hash that was already computed
    while the page was clear; it is moved here at the moment the page is blocked, in the same step
    that deletes it from `FrameHash`. Where no hash existed, the case simply has none.

    Retention is the incident's: the rows cascade with the case `safety_retention` deletes.
    """

    incident = models.ForeignKey(
        SafetyIncident, on_delete=models.CASCADE, related_name="retained_hashes"
    )
    pdq = models.BinaryField(max_length=PDQ_HASH_BYTES)
    quality = models.PositiveSmallIntegerField(help_text="PDQ quality score, 0-100.")
    source_kind = models.CharField(
        max_length=16,
        blank=True,
        help_text="Media kind the hash was computed from. No URL, no bytes, no page content.",
    )
    retained_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "safety incident hashes"
        constraints = (
            models.UniqueConstraint(fields=("incident", "pdq"), name="uniq_incident_pdq"),
            models.CheckConstraint(
                condition=Exact(Length("pdq"), PDQ_HASH_BYTES), name="incidenthash_pdq_32_bytes"
            ),
            models.CheckConstraint(
                condition=models.Q(quality__lte=100), name="incidenthash_quality"
            ),
        )

    def __str__(self) -> str:
        return f"incident {self.incident_id} hash {self.pk}"


class Media(models.Model):
    """An image or clip a page references: a video's preview assets, or a photo post itself.

    Also the media worker's fetch queue: claimed with SELECT ... FOR UPDATE SKIP LOCKED.
    """

    class Kind(models.TextChoices):
        POSTER = "poster"
        FRAME = "frame"
        SPRITE = "sprite"
        PREVIEW = "preview"
        # The work itself rather than a preview of a video: one full-resolution image that is the
        # whole post (issue #69; first source site11.example). Decoded and hashed exactly like a
        # POSTER — the kind records what the picture *is*, it does not change the pipeline.
        PHOTO = "photo"
        # Frames sampled from the page's own player by the browser capture worker (ADR 0016).
        # There is nothing to fetch: the row exists to hang FrameHash rows off and to record that
        # this page was captured. Its `url` is the page's own URL with a `#browser-capture`
        # fragment, and `eligible_at` stays NULL forever, so no fetch path can ever request it.
        CAPTURE = "capture"

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"
        SKIPPED = "skipped"
        # The media host's own robots.txt disallows this URL (or a redirect hop of it) for our
        # User-Agent: never requested, never retried (ADR 0007 amendment 2026-09-17). Frame hashes
        # stored before the rule existed are kept.
        ROBOTS_BLOCKED = "robots_blocked"

    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="media")
    kind = models.CharField(max_length=16, choices=Kind)
    url = models.URLField(max_length=2000)
    eligible_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When this media became eligible to fetch (ADR 0018): the detail page itself was "
            "fetched, parsed and scored clearly safe. NULL means ineligible — the URL is "
            "recorded, but no request may be made for it."
        ),
    )
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    # The sprite sheet's tile grid (issue #14), when an adapter read it off the site's own markup
    # (JSON config, a WebVTT thumbnail track's `#xywh=` cues): "believe the site" is exact and
    # free, so the media worker uses this instead of guessing from the fetched image's geometry
    # (`matching.sprite_grid`). Both set or both NULL; only meaningful for `Kind.SPRITE`, but not
    # constrained to it — an adapter's declaration is trusted regardless of kind bookkeeping.
    sprite_columns = models.PositiveSmallIntegerField(null=True, blank=True)
    sprite_rows = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status, default=Status.PENDING, db_default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0, db_default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, help_text="Error or skip reason of the last fetch.")
    content_type = models.CharField(max_length=255, blank=True)
    byte_size = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "media"
        constraints = (
            models.UniqueConstraint(fields=("page", "url"), name="uniq_page_media"),
            models.CheckConstraint(
                condition=models.Q(sprite_columns__isnull=True, sprite_rows__isnull=True)
                | models.Q(sprite_columns__isnull=False, sprite_rows__isnull=False),
                name="media_sprite_grid_both_or_neither",
            ),
        )
        indexes = (
            models.Index(
                fields=("status", "next_attempt_at", "-created_at"), name="media_claim_idx"
            ),
            # The media claim's media side: only rows still waiting and allowed to be fetched,
            # per page (review PERF). DONE/SKIPPED rows — most of the table — are not in it.
            models.Index(
                fields=("page",),
                name="media_claimable_idx",
                condition=models.Q(status="pending", eligible_at__isnull=False),
            ),
        )

    def __str__(self) -> str:
        return f"{self.kind} {self.url}"


class FrameHash(models.Model):
    """A 256-bit PDQ hash of one frame (or the single image) of a media asset.

    Since ADR 0037 it may also carry a 512-bit **sign sketch** of the same picture: a second,
    independent signal that survives the asymmetric re-frame PDQ dies on. It is a candidate
    generator, never evidence, and it is **not retroactive** — the picture is discarded after
    hashing, so only media fetched while embeddings were on has one. An empty `embedding` means
    "no information", never "no match".
    """

    media = models.ForeignKey(Media, on_delete=models.CASCADE, related_name="hashes")
    frame_index = models.PositiveIntegerField(default=0, db_default=0)
    pdq = models.BinaryField(max_length=PDQ_HASH_BYTES)
    quality = models.PositiveSmallIntegerField(help_text="PDQ quality score, 0-100.")
    embedding = models.BinaryField(
        max_length=EMBEDDING_SKETCH_BYTES,
        null=True,
        blank=True,
        help_text="512-bit sign sketch of this frame (ADR 0037); empty = never embedded.",
    )
    embedding_pipeline = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_default="",
        help_text="Recipe that produced the sketch; compared only with the same tag (ADR 0022).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("media", "frame_index"), name="uniq_media_frame"),
            models.CheckConstraint(
                condition=Exact(Length("pdq"), PDQ_HASH_BYTES), name="framehash_pdq_32_bytes"
            ),
            models.CheckConstraint(condition=models.Q(quality__lte=100), name="framehash_quality"),
            # A sketch is 64 bytes and always says which recipe made it; neither half alone is
            # usable, so the pair is enforced here rather than trusted to callers.
            models.CheckConstraint(
                condition=models.Q(embedding__isnull=True, embedding_pipeline="")
                | (
                    Exact(Length("embedding"), EMBEDDING_SKETCH_BYTES)
                    & ~models.Q(embedding_pipeline="")
                ),
                name="framehash_embedding_64_bytes_with_pipeline",
            ),
        )

    def __str__(self) -> str:
        return f"{self.media_id}#{self.frame_index}"


class SiteCreatorBreadcrumb(models.Model):
    """What a site roughly carries for one creator: a cheap trail kept for every creator, target
    or not (issue #71), so a creator who signs later can be hunted where they already are.

    No URLs and no page content — counts and dates only. `page_count` counts pages of the site
    linked to the creator (`Page.creators`), active or not. Kept current by the crawler whenever
    it links a page, and rebuilt from the index by `deactivate_untargeted --apply`.
    """

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="breadcrumbs")
    creator = models.ForeignKey(Creator, on_delete=models.CASCADE, related_name="breadcrumbs")
    page_count = models.PositiveIntegerField(default=0, db_default=0)
    first_seen = models.DateTimeField(help_text="Earliest first_seen of a linked page.")
    last_seen = models.DateTimeField(help_text="Latest last_seen of a linked page.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("site", "creator"), name="uniq_site_creator_breadcrumb"
            ),
        )
        indexes = (models.Index(fields=("creator", "-last_seen"), name="breadcrumb_creator_idx"),)

    def __str__(self) -> str:
        return f"{self.creator_id} on {self.site_id}: {self.page_count} pages"


class ProspectSampleCheck(models.Model):
    """A staff verdict on one page of a prospect's verification sample (`prospect_report`).

    Before any outreach, a reviewer opens the sampled page ids in the moderation UI and records
    whether the page really carries the prospect's content. The report states the precision of
    these verdicts next to its headline number (docs/prospect-research.md). Attribution is by
    names on pages and PDQ clusters only — never by faces or any biometric processing.
    """

    class Verdict(models.TextChoices):
        CORRECT = "correct", "Correct: the prospect's content"
        WRONG = "wrong", "Wrong: not theirs (name collision, compilation, other)"
        UNSURE = "unsure"

    creator = models.ForeignKey(
        Creator, on_delete=models.CASCADE, related_name="prospect_sample_checks"
    )
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="prospect_sample_checks")
    verdict = models.CharField(max_length=16, choices=Verdict)
    note = models.TextField(blank=True, help_text="Why. Never page content, no personal data.")
    checked_by = models.CharField(max_length=150, blank=True, help_text="Reviewer username.")
    checked_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("creator", "page"), name="uniq_prospect_sample_check"),
        )

    def __str__(self) -> str:
        return f"{self.creator_id} / page {self.page_id}: {self.verdict}"
