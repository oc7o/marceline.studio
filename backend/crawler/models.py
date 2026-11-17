"""Crawl queue and the audit log of LLM calls made while crawling."""

from django.db import models

from catalog.models import Site
from crawler.discovery.kinds import Coverage


class CrawlTask(models.Model):
    """One URL to fetch. Workers claim pending tasks with SELECT ... FOR UPDATE SKIP LOCKED."""

    class Kind(models.TextChoices):
        LISTING = "listing"
        DETAIL = "detail"
        SEARCH = "search"

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"
        SKIPPED = "skipped"

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="crawl_tasks")
    url = models.URLField(max_length=2000, unique=True)
    kind = models.CharField(max_length=16, choices=Kind)
    status = models.CharField(
        max_length=16, choices=Status, default=Status.PENDING, db_default=Status.PENDING
    )
    priority = models.IntegerField(default=0, db_default=0)
    attempts = models.PositiveSmallIntegerField(default=0, db_default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    discovered_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="discovered"
    )
    decision_reason = models.TextField(blank=True, help_text="Why the LLM queued or skipped it.")
    last_error = models.TextField(blank=True)
    context = models.JSONField(
        default=dict,
        db_default={},
        blank=True,
        help_text="Hints from the linking page, e.g. listing thumbnails.",
    )
    fetched_at = models.DateTimeField(
        null=True, blank=True, help_text="Last time the URL was requested (counts toward budget)."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = (
            models.Index(
                fields=("status", "next_attempt_at", "-priority"), name="crawltask_claim_idx"
            ),
            models.Index(fields=("site", "fetched_at"), name="crawltask_site_fetched_idx"),
        )

    def __str__(self) -> str:
        return f"{self.kind} {self.url}"


class LLMCall(models.Model):
    """Every LLM request made by the crawler: cost, input fingerprint and validated output.

    Also every request that was **not** made: a repair the pre-transmission safety gate refused
    (ADR 0042 amendment 2026-09-18) is a row with an empty `input_sha256`, no tokens, no cost, no
    `output` and an `error` beginning `refused_` that names the rule and never the page's text.
    `crawler.repair.calls_today` excludes those rows, so a refusal spends no daily budget.
    """

    class Purpose(models.TextChoices):
        #: Adapter repair (ADR 0042, issue #22): fields re-read from a broken adapter's page text.
        ADAPTER_REPAIR = "adapter_repair"

    task = models.ForeignKey(
        CrawlTask, null=True, blank=True, on_delete=models.SET_NULL, related_name="llm_calls"
    )
    #: Which site the call was about. Kept beside `task` rather than derived from it: a task row
    #: can be deleted (`SET_NULL`) and the per-site spend cap has to go on counting regardless.
    site = models.ForeignKey(
        Site, null=True, blank=True, on_delete=models.SET_NULL, related_name="llm_calls"
    )
    purpose = models.CharField(max_length=32, choices=Purpose, blank=True, db_index=True)
    model = models.CharField(max_length=100)
    input_tokens = models.PositiveIntegerField(default=0, db_default=0)
    output_tokens = models.PositiveIntegerField(default=0, db_default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0, db_default=0)
    input_sha256 = models.CharField(max_length=64, db_index=True)
    output = models.JSONField(null=True, blank=True)
    decision = models.TextField(blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "LLM call"

    def __str__(self) -> str:
        return f"{self.model} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


class SearchCall(models.Model):
    """One HTTP request to the search provider, including failed requests.

    Two records in one row, which is why it has the retention rule it has (ADR 0048, issue #103):
    it is the **money ledger** ADR 0013's spend guard reads before every paid request, and it is a
    record that this named person was searched for. The row is therefore never deleted — `query` is
    blanked after `SEARCH_QUERY_RETENTION_DAYS` and `query_scrubbed_at` stamped, which leaves an
    accounting line and no name (`crawler/retention.py`).
    """

    engine = models.CharField(
        max_length=16, choices=(("google", "Google"), ("bing", "Bing")), default="google"
    )
    query = models.TextField(
        blank=True,
        help_text="The search text. Blanked by `research_retention` once it is past its period, "
        "and by an index suppression naming the person (ADR 0048).",
    )
    query_scrubbed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the query text was removed. The rest of the row is the spend ledger and "
        "stays.",
    )
    location = models.PositiveIntegerField(default=2840)
    language = models.CharField(max_length=16, default="en")
    mode = models.CharField(max_length=16, choices=(("standard", "Standard"), ("live", "Live")))
    result_count = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    status = models.CharField(max_length=32)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self) -> str:
        return f"{self.engine}: {self.query}"


class SourceCandidate(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "proposed"
        APPROVED = "approved"
        REJECTED = "rejected"
        HOSTER = "hoster"
        IGNORED = "ignored"
        # Torrent/P2P sources (ADR 0013 amendment 2026-09-17): not a classical file hoster and
        # not crawled by our page adapters, but not dismissed either — a distinct finding kept
        # visible for a later decision, alongside `hoster` and the mirror note under `ignored`.
        TORRENT = "torrent"
        # ADR 0013 amendment 2026-09-17: a candidate whose domain already exists as a `Site` is
        # that site (proposed -> approved -> built already, just never closed out here), never a
        # mirror of itself. `Site.enabled` doesn't matter, only that the row exists.
        ONBOARDED = "onboarded"

    class Kind(models.TextChoices):
        TUBE = "tube"
        FILE_HOSTER = "file_hoster"
        FORUM = "forum"
        SOCIAL = "social"
        TORRENT = "torrent"
        # Issue #118: the kinds independent creators' content is republished in, as far as a
        # bounded probe can tell them apart (`crawler.discovery.kinds`,
        # docs/research/creator-leak-sources.md). `NAME_INDEX` is a site organised around
        # people's names rather than uploads; `GALLERY` is one whose posts are image sets.
        NAME_INDEX = "name_index"
        GALLERY = "gallery"
        UNKNOWN = "unknown"

    domain = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.PROPOSED)
    kind = models.CharField(max_length=16, choices=Kind, default=Kind.UNKNOWN)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    search_hits = models.PositiveIntegerField(default=0)
    distinct_creators = models.PositiveIntegerField(default=0)
    best_rank = models.PositiveIntegerField(null=True, blank=True)
    outlink_pages = models.PositiveIntegerField(default=0)
    evidence = models.JSONField(default=dict, blank=True)
    probed_at = models.DateTimeField(null=True, blank=True)
    robots_allows = models.BooleanField(null=True, blank=True)
    http_status = models.PositiveIntegerField(null=True, blank=True)
    blocked_reason = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=1000, blank=True)
    cms_guess = models.CharField(max_length=16, default="unknown")
    has_search = models.BooleanField(default=False)
    video_links_seen = models.PositiveIntegerField(default=0)
    #: Structure counters from the same one homepage the probe already fetched (issue #118): no
    #: extra request, counts only, never a URL or a title.
    profile_links_seen = models.PositiveIntegerField(
        default=0, db_default=0, help_text="Same-site links into a person's own page."
    )
    forum_links_seen = models.PositiveIntegerField(
        default=0, db_default=0, help_text="Same-site links into a thread or board."
    )
    gallery_images_seen = models.PositiveIntegerField(
        default=0, db_default=0, help_text="Same-template images of one post or gallery."
    )
    #: What our own rules allow for this candidate (`crawler.discovery.kinds.Coverage`). Written
    #: only while the row is unreviewed, so a reviewed decision is never contradicted later.
    coverage = models.CharField(
        max_length=20,
        choices=[(value.value, value.value) for value in Coverage],
        default=Coverage.UNKNOWN,
        db_default=Coverage.UNKNOWN.value,
    )
    notes = models.TextField(blank=True)

    class Meta:
        constraints = (
            # A candidate our own rules forbid must never reach onboarding (issue #118). The
            # admin action says so with a message; this says so whatever writes the row.
            models.CheckConstraint(
                condition=~models.Q(status="approved")
                | ~models.Q(coverage__in=[Coverage.NOT_COVERABLE, Coverage.PARKED]),
                name="source_candidate_not_coverable_never_approved",
            ),
        )

    def __str__(self) -> str:
        return self.domain


class CandidateCreator(models.Model):
    """Exact creator dedupe independent of capped display evidence."""

    candidate = models.ForeignKey(SourceCandidate, on_delete=models.CASCADE)
    creator = models.ForeignKey("catalog.Creator", on_delete=models.CASCADE)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("candidate", "creator"), name="candidate_creator_unique"
            ),
        )

    def __str__(self) -> str:
        return f"{self.candidate_id}: creator {self.creator_id}"


class CandidatePage(models.Model):
    """Exact linking-page dedupe across repeated mining runs."""

    candidate = models.ForeignKey(SourceCandidate, on_delete=models.CASCADE)
    page = models.ForeignKey("index.Page", on_delete=models.CASCADE)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("candidate", "page"), name="candidate_page_unique"),
        )

    def __str__(self) -> str:
        return f"{self.candidate_id}: page {self.page_id}"


class TelegramChannelCandidate(models.Model):
    """A Telegram channel (or private invite) linked from indexed pages. Stored, never crawled.

    Approval records a human decision only; it enables no crawling (ADR 0013).
    """

    class Status(models.TextChoices):
        PROPOSED = "proposed"
        IGNORED = "ignored"
        APPROVED = "approved"
        REJECTED = "rejected"

    private_invite = models.BooleanField(
        default=False, help_text="Private invite link: off-limits, never followed or crawled."
    )
    identity = models.CharField(
        max_length=64,
        help_text="Lowercase public username, or SHA-256 hex of a private invite code.",
    )
    status = models.CharField(max_length=16, choices=Status, default=Status.PROPOSED)
    linking_pages = models.PositiveIntegerField(default=0)
    distinct_creators = models.PositiveIntegerField(default=0)
    distinct_sites = models.PositiveIntegerField(default=0)
    sites = models.JSONField(default=list, blank=True, help_text="Linking site domains (capped).")
    sample_pages = models.JSONField(
        default=list, blank=True, help_text="Linking page URLs (capped)."
    )
    sample_post_ids = models.JSONField(
        default=list, blank=True, help_text="Post ids seen in links (capped)."
    )
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("private_invite", "identity"), name="telegram_candidate_identity_unique"
            ),
            models.CheckConstraint(
                condition=~models.Q(identity=""), name="telegram_candidate_identity_set"
            ),
            models.CheckConstraint(
                condition=~models.Q(private_invite=True, status="approved"),
                name="telegram_candidate_invite_not_approved",
            ),
        )
        indexes = (
            models.Index(
                fields=("-linking_pages", "-distinct_creators"), name="telegram_candidate_rank_idx"
            ),
        )

    def __str__(self) -> str:
        if self.private_invite:
            return f"private invite {self.identity[:12]}"
        return f"t.me/{self.identity}"


class TelegramChannelPage(models.Model):
    """Exact linking-page dedupe for Telegram channel candidates."""

    candidate = models.ForeignKey(TelegramChannelCandidate, on_delete=models.CASCADE)
    page = models.ForeignKey("index.Page", on_delete=models.CASCADE)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("candidate", "page"), name="telegram_candidate_page_unique"
            ),
        )

    def __str__(self) -> str:
        return f"{self.candidate_id}: page {self.page_id}"


class AdapterParseStat(models.Model):
    """What one site's adapter yielded, per hour: the raw material of breakage detection (#22).

    One row per site and hour, written by the crawl worker with a single
    ``INSERT … ON CONFLICT DO UPDATE`` so every worker and every runtime thread can add to the
    same bucket without a lock. Only **detail** parses are counted, and only those that reached
    the adapter at all: a 404, a block, a robots.txt refusal or a network error says nothing about
    the parser. A page the adapter reports as removed is not counted either — that is the adapter
    working.

    Counters, not content: no URL, no title, no page text ever lands here.
    """

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="adapter_parse_stats")
    hour = models.DateTimeField(db_index=True, help_text="UTC hour the parses happened in.")
    parses = models.PositiveIntegerField(default=0, db_default=0)
    #: `parse()` returned no page at all for a detail URL that was fetched and not reported removed.
    no_page = models.PositiveIntegerField(default=0, db_default=0)
    no_title = models.PositiveIntegerField(default=0, db_default=0)
    no_media = models.PositiveIntegerField(default=0, db_default=0)
    #: Reported for context only — never alarmed on. See `crawler.adapter_health`.
    no_duration = models.PositiveIntegerField(default=0, db_default=0)
    no_performers = models.PositiveIntegerField(default=0, db_default=0)
    #: Of `parses`, how many the LLM repair (ADR 0042) filled a field in.
    llm_repaired = models.PositiveIntegerField(default=0, db_default=0)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("site", "hour"), name="adapter_parse_stat_unique"),
        )
        indexes = (models.Index(fields=("site", "-hour"), name="adapter_stat_site_hour_idx"),)

    def __str__(self) -> str:
        return f"{self.site_id} @ {self.hour:%Y-%m-%d %H}: {self.parses} parse(s)"


class AdapterHealth(models.Model):
    """The current verdict on one site's adapter (issue #22), and what it was decided on.

    Written by `crawler.adapter_health.evaluate` (CLI ``adapter_health``, scheduler job
    ``adapter_health``), read by the worker to decide whether the LLM repair may run at all. One
    row per site; `status` is the operator-visible answer to "is this parser still working".
    """

    class Status(models.TextChoices):
        #: Too few detail parses in the window to say anything.
        UNKNOWN = "unknown"
        OK = "ok"
        #: The blind share crossed `adapter_health.BROKEN_SHARE` and has not recovered.
        BROKEN = "broken"

    site = models.OneToOneField(Site, on_delete=models.CASCADE, related_name="adapter_health")
    status = models.CharField(
        max_length=16, choices=Status, default=Status.UNKNOWN, db_default=Status.UNKNOWN
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    broken_since = models.DateTimeField(
        null=True, blank=True, help_text="First evaluation that found it broken; cleared on repair."
    )
    #: Detail parses in the recent window the verdict was computed from.
    parses = models.PositiveIntegerField(default=0, db_default=0)
    #: Share of those that yielded no page, no title or no media, 0..1.
    blind_share = models.FloatField(default=0.0, db_default=0.0)
    #: The same share over the older baseline window, for "did it jump".
    baseline_blind_share = models.FloatField(null=True, blank=True)
    detail = models.TextField(blank=True, help_text="Why, in one line. Counters only.")
    notified_at = models.DateTimeField(
        null=True, blank=True, help_text="When the operator was last mailed about this break."
    )
    llm_repairs_today = models.PositiveIntegerField(
        default=0, db_default=0, help_text="Informational: repair calls in the last 24 h."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "adapter health"

    def __str__(self) -> str:
        return f"{self.site_id}: {self.status}"
