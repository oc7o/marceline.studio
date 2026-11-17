from django.db import models


class JobRun(models.Model):
    """Persistent state of one periodic scheduler job (one row per job name)."""

    class Status(models.TextChoices):
        OK = "ok"
        FAILED = "failed"
        SKIPPED = "skipped"

    name = models.CharField(max_length=64, unique=True)
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_finished_at = models.DateTimeField(null=True, blank=True)
    last_succeeded_at = models.DateTimeField(
        null=True, blank=True, help_text="Start time of the last run that finished ok."
    )
    last_status = models.CharField(max_length=16, choices=Status, blank=True)
    last_message = models.TextField(blank=True)

    class Meta:
        ordering = ("name",)
        constraints = (
            models.CheckConstraint(
                condition=models.Q(last_status__in=("", "ok", "failed", "skipped")),
                name="jobrun_last_status_valid",
            ),
        )

    def __str__(self) -> str:
        return self.name


class HostSlot(models.Model):
    """One request slot per host, shared by every crawl thread and process (ADR 0030 § 2).

    Written only by `core.host_slots.DbHostGate`, in short statements on the database clock
    (`clock_timestamp()`), so container clock skew cannot shorten a delay. `key` is
    `core.http.host_key`: lower-case hostname, a leading `www.` removed.
    """

    key = models.CharField(primary_key=True, max_length=255)
    next_allowed_at = models.DateTimeField(
        help_text="Earliest start of the next request: previous start + that request's delay."
    )
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_delay_s = models.FloatField(
        default=0.0, db_default=0.0, help_text="Delay the last request asked for."
    )
    min_delay_s = models.FloatField(
        default=0.0,
        db_default=0.0,
        help_text="Operator floor for every request to this host, in seconds (0: callers decide).",
    )
    leased_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="A request is in flight; expires by itself if its holder dies.",
    )
    lease_owner = models.CharField(max_length=128, blank=True)
    last_lane = models.CharField(max_length=16, blank=True)
    waiting_lane = models.CharField(max_length=16, blank=True)
    waiting_until = models.DateTimeField(null=True, blank=True)
    penalty_until = models.DateTimeField(
        null=True, blank=True, help_text="Host-wide backoff after 429, 5xx or network errors."
    )
    penalty_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("key",)
        constraints = (
            models.CheckConstraint(
                condition=models.Q(min_delay_s__gte=0), name="hostslot_min_delay_not_negative"
            ),
        )

    def __str__(self) -> str:
        return self.key
