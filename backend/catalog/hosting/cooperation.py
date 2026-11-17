"""How likely a takedown notice to a provider is to work, as a band. Pure; thresholds are named.

The raw history (`catalog.HosterContact`: every notice, when it was answered, what came of it)
is internal and stays in the model and the admin. **The band is internal too** since 2026-09-17:
a band reads as a prediction, so a customer sees `catalog.hosting.history.customer_history`
instead — a rounded, past-tense response history (ADR 0027 amendment 2026-09-17).

The band is read from **decided** notices (any outcome but `awaiting`): a notice sent yesterday is
not a failure yet. Below `MIN_DECIDED_NOTICES` the answer is `unknown` — two notices are an
anecdote, not a track record. Today no notices exist, so every provider is `unknown`.

- `removed` counts as success, `refused` and `no_response` as failure;
- `forwarded` (a CDN passing the notice to the origin host) is neither: when it is the most common
  outcome, the provider's own behaviour is not what decides, and the band is `mixed`.
"""

from dataclasses import dataclass
from typing import Literal

#: Ordered from worst to best, `unknown` first. The UI may rely on this order.
TakedownChance = Literal["unknown", "bad", "mixed", "good", "very_good", "exemplary"]
TAKEDOWN_CHANCES: tuple[TakedownChance, ...] = (
    "unknown",
    "bad",
    "mixed",
    "good",
    "very_good",
    "exemplary",
)

#: Fewer decided notices than this and the band is `unknown`.
MIN_DECIDED_NOTICES = 3
#: Share of decided notices that led to a removal, at or above which a band applies.
EXEMPLARY_MIN_REMOVAL_RATE = 0.95
VERY_GOOD_MIN_REMOVAL_RATE = 0.80
GOOD_MIN_REMOVAL_RATE = 0.60
MIXED_MIN_REMOVAL_RATE = 0.30
#: When more than this share of decided notices were forwarded, the band is `mixed`.
MIXED_MIN_FORWARDED_RATE = 0.50


@dataclass(frozen=True)
class Cooperation:
    """Internal outcome counts of our notices to one provider."""

    notices_sent: int = 0
    decided: int = 0
    removed: int = 0
    forwarded: int = 0


def takedown_chance(history: Cooperation) -> TakedownChance:
    if history.decided < MIN_DECIDED_NOTICES:
        return "unknown"
    removal_rate = history.removed / history.decided
    if history.forwarded / history.decided > MIXED_MIN_FORWARDED_RATE:
        return "mixed"
    if removal_rate >= EXEMPLARY_MIN_REMOVAL_RATE:
        return "exemplary"
    if removal_rate >= VERY_GOOD_MIN_REMOVAL_RATE:
        return "very_good"
    if removal_rate >= GOOD_MIN_REMOVAL_RATE:
        return "good"
    if removal_rate >= MIXED_MIN_REMOVAL_RATE:
        return "mixed"
    return "bad"
