"""What development records when it refuses a relayed attestation (ADR 0044 §9, amendment
2026-09-20).

One table, registered with the project app by `marceline/models.py`, written by one function
(`marceline/uploads/attestation.py` `record`). It exists because the public host's relay credential
is a **write** capability on development: everything it does successfully leaves a `MediaUpload`
row (organisation, user, origin `relay`, sizes, timestamps, outcome), but until now everything it
did *unsuccessfully* left nothing at all — a `web` naming organisations and users that are not
entitled customers was invisible.

**It is a counter, not a journal.** One row per hour per reason, and nothing else: no
organisation, no user, no upload, no title, no address. Two reasons, in order of importance.
A row that named the attested pair would grow without bound under exactly the caller this table
watches — the credential can name any integer it likes — and the counter cannot. And the pair is
not what the operator needs: the alarm is *"the public host attested something that is not an
entitled customer, N times since X"*, and the investigation that follows is of the host, not of
the row.

`marceline/0030` makes it append-only in the database: DELETE is refused outright and the only
UPDATE that passes is this counter going up.
"""

from django.db import models
from django.utils import timezone


class RelayRefusal(models.Model):
    """How often development refused the public host's attestation, by hour and by reason."""

    class Reason(models.TextChoices):
        #: The two headers were absent, empty or not integers.
        MALFORMED = "malformed", "the attestation did not parse"
        #: No `accounts.Membership` links exactly that user to exactly that organisation.
        NO_MEMBERSHIP = "no_membership", "the attested pair is not a customer of ours"
        #: The pair exists, but the account is switched off — its own session would be refused.
        INACTIVE = "inactive", "the attested account is not active"
        #: This machine is not taking files at all, so no browser could have done this either.
        UNAVAILABLE = "unavailable", "uploads are not available here"
        #: The relay asked to start an upload for more organisations than the ceiling allows.
        SPREAD = "spread", "too many organisations in the window"

    hour = models.DateTimeField(help_text="The hour these attempts fell in, truncated (UTC).")
    reason = models.CharField(max_length=16, choices=Reason)
    attempts = models.PositiveIntegerField(default=1)
    first_at = models.DateTimeField(default=timezone.now)
    last_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "relay refusal"
        constraints = (
            models.UniqueConstraint(fields=("hour", "reason"), name="relay_refusal_hour_reason"),
        )
        indexes = (models.Index(fields=("-hour",)),)

    def __str__(self) -> str:
        return f"{self.reason} x{self.attempts} ({self.hour:%Y-%m-%d %H}:00)"
