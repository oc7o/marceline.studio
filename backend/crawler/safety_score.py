"""Per-page safety score: how confident we are that a page's own text is clearly safe.

Pure functions, no database and no clock (DB workflows live in `crawler.services`). ADR 0011's
term check answers "is this page flagged?"; this module answers the narrower question ADR 0018
asks before any picture is fetched: "is this page's own text clearly safe enough to fingerprint
it?". Only pages at or above `DEFAULT_MIN_SCORE` have their media fetched and hashed; everything
else stays text-only (indexed and matchable by title and duration, no hashes).

Every signal only ever *lowers* the score, so a page we know little about never scores high. The
inputs are the page's own text (title, tags, performers, extracted text), near misses that are not
terms, how much text there was to judge at all, whether the text is in a language the term list
covers, and a per-site risk factor that rises with recent `block` incidents on that site.

The score is deliberately not a probability. It is an ordering with documented, configurable
cut-offs; see `docs/adr/0018-media-safety-gate.md` for the defaults and why they were chosen.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from catalog import safety_rules
from crawler.safety import CompiledTerm, Verdict, normalize

# Score range. 1.0 = nothing at all speaks against the page; 0.0 = a hard deny.
MAX_SCORE = 1.0
MIN_SCORE = 0.0
# "Clearly safe": media of pages below this stay unfetched. Conservative on purpose — a missed
# fingerprint costs recall, a wrong fetch is the risk the whole gate exists for. At this default a
# page passes only when nothing fired or a single mild signal did; any two independent concerns,
# or one strong one (an explicit under-age pattern), hold its media back. Measured on the live
# index (2026-09-16): 183 of 12,562 pages gated (1.5 %), no confirmed leak page loses its hashes.
# Keep in step with `SAFETY_GATE_MIN_SCORE` in marceline/settings.py (a test asserts they agree).
DEFAULT_MIN_SCORE = 0.80

# Reason codes, stored on `index.Page.safety_score_reason`. The *deciding* reason is the single
# heaviest penalty; `PageScore.signals` keeps the full list.
BLOCK_TERM = "block_term"
REVIEW_TERM = "review_term"
AGE_PATTERN = "age_pattern"
MINOR_CONTEXT = "minor_context"
BARE_LOW_AGE = "bare_low_age"
UNCOVERED_LANGUAGE = "uncovered_language"
THIN_TEXT = "thin_text"
SITE_RISK = "site_risk"
CLEARLY_SAFE = "clearly_safe"

# Penalties. Tuned against the live index (2026-09-16, 12,570 pages) so that the specific minor
# signals fire on 0-4 % of pages per site while generic leak-site vocabulary does not fire at all.
PENALTIES: dict[str, float] = {
    AGE_PATTERN: 0.60,  # an explicit under-18 age in a unit the block terms do not cover
    MINOR_CONTEXT: 0.15,  # school / family / "young" wording, per distinct cluster, capped below
    BARE_LOW_AGE: 0.10,  # a bare 11-17 token: often a resolution or a date, so only a nudge
    UNCOVERED_LANGUAGE: 0.15,  # text in a script the term list barely covers
    THIN_TEXT: 0.10,  # almost nothing was parsed, so the term check had almost nothing to judge
}
# At most this much may be subtracted for minor-context clusters together.
MAX_MINOR_CONTEXT_PENALTY = 0.30
# Per-site risk: each recent `block` incident on the site costs this much, capped.
SITE_RISK_PER_BLOCK = 0.10
MAX_SITE_RISK_PENALTY = 0.30
# Fewer than this many normalized words of page text counts as "thin".
MIN_TEXT_WORDS = 4

# Near misses. These are deliberately *not* `catalog.SafetyTerm`s: they never block or queue a
# review on their own, they only hold media back. They run over the same normalized text as terms
# (lower-case, punctuation collapsed to single spaces), so word edges are plain spaces.
#
# Ages 1-17 with a unit the seeded block regex does not cover (it handles yo / yrs old / years old).
_AGE_NEAR_MISS = re.compile(
    r"(?<!\w)(?:1[0-7]|[1-9])\s?(?:jahre|jahren|jaar|anni|ans|lat|let|лет|года|годика)(?!\w)"
)
# A bare 11-17 standing alone. Noisy (resolutions, dates, part numbers), hence the small penalty.
_BARE_LOW_AGE = re.compile(r"(?<!\w)1[1-7](?!\w)")
# Minor-context clusters. Each cluster counts once, however often it matches.
_MINOR_CONTEXT: dict[str, re.Pattern[str]] = {
    "school": re.compile(
        r"(?<!\w)(?:flagged term|highschool|high school|classroom|"
        r"schulmadchen|schuler\w*|школьн\w*)(?!\w)"
    ),
    "family": re.compile(
        r"(?<!\w)(?:daughter|stepdaughter|step daughter|niece|tochter|stieftochter|дочь|дочк\w*)"
        r"(?!\w)"
    ),
    "young": re.compile(
        r"(?<!\w)(?:babyface|baby face|childlike|child like|kiddie|jung\w*|малышк\w*|"
        r"молоденьк\w*)(?!\w)"
    ),
}
# Scripts the seeded term list covers (English, German, Russian). Text that is mostly outside
# them is judged by a term list that was never written for it.
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[a-z]")
# Share of letters that must be Cyrillic before the Russian terms are the ones doing the work.
_CYRILLIC_SHARE = 0.30


@dataclass(frozen=True)
class SiteRisk:
    """What the site itself contributes, independent of this page's text."""

    domain: str = ""
    recent_blocks: int = 0

    @property
    def penalty(self) -> float:
        return min(SITE_RISK_PER_BLOCK * max(self.recent_blocks, 0), MAX_SITE_RISK_PENALTY)


NO_SITE_RISK = SiteRisk()


@dataclass(frozen=True)
class PageScore:
    """The score of one page, the deciding reason, and every signal that fired."""

    score: float
    reason: str
    signals: tuple[str, ...] = ()

    def is_clearly_safe(self, minimum: float = DEFAULT_MIN_SCORE) -> bool:
        return self.score >= minimum


@dataclass(frozen=True)
class _Penalty:
    reason: str
    amount: float
    detail: str = ""

    @property
    def signal(self) -> str:
        return f"{self.reason}:{self.detail}" if self.detail else self.reason


@dataclass
class _Accumulator:
    penalties: list[_Penalty] = field(default_factory=list)

    def add(self, reason: str, amount: float, detail: str = "") -> None:
        if amount > 0:
            self.penalties.append(_Penalty(reason, amount, detail))


def cyrillic_share(text: str) -> float:
    """Share of the letters in `text` that are Cyrillic (0.0 when there are no letters)."""
    cyrillic = len(_CYRILLIC.findall(text))
    latin = len(_LATIN.findall(text))
    total = cyrillic + latin
    return cyrillic / total if total else 0.0


def score_page(
    texts: Iterable[str],
    verdict: Verdict,
    *,
    site_risk: SiteRisk = NO_SITE_RISK,
) -> PageScore:
    """Score one page from its own text and the term verdict for that same text.

    `verdict` must come from `crawler.safety.check` over `texts`: a flagged page is never
    "clearly safe", so `block` and `review` short-circuit to 0.0 and keep their hard deny.
    """
    if any(safety_rules.blocks(term.action) for term in verdict.matches):
        return PageScore(MIN_SCORE, BLOCK_TERM, (BLOCK_TERM,))
    if verdict.matches:
        return PageScore(MIN_SCORE, REVIEW_TERM, (REVIEW_TERM,))

    haystack = "\n".join(normalize(text) for text in texts if text)
    found = _Accumulator()

    if _AGE_NEAR_MISS.search(haystack):
        found.add(AGE_PATTERN, PENALTIES[AGE_PATTERN])
    clusters = [name for name, pattern in _MINOR_CONTEXT.items() if pattern.search(haystack)]
    if clusters:
        found.add(
            MINOR_CONTEXT,
            min(PENALTIES[MINOR_CONTEXT] * len(clusters), MAX_MINOR_CONTEXT_PENALTY),
            ",".join(sorted(clusters)),
        )
    if _BARE_LOW_AGE.search(haystack):
        found.add(BARE_LOW_AGE, PENALTIES[BARE_LOW_AGE])
    if cyrillic_share(haystack) >= _CYRILLIC_SHARE:
        found.add(UNCOVERED_LANGUAGE, PENALTIES[UNCOVERED_LANGUAGE], "cyrillic")
    if len(haystack.split()) < MIN_TEXT_WORDS:
        found.add(THIN_TEXT, PENALTIES[THIN_TEXT])
    found.add(SITE_RISK, site_risk.penalty, str(site_risk.recent_blocks))

    total = sum(penalty.amount for penalty in found.penalties)
    score = max(MIN_SCORE, min(MAX_SCORE, MAX_SCORE - total))
    if not found.penalties:
        return PageScore(score, CLEARLY_SAFE)
    deciding = max(found.penalties, key=lambda penalty: penalty.amount)
    signals = tuple(penalty.signal for penalty in sorted(found.penalties, key=lambda p: p.reason))
    return PageScore(score, deciding.reason, signals)


def score_texts(
    texts: Sequence[str],
    terms: Sequence[CompiledTerm],
    *,
    site_risk: SiteRisk = NO_SITE_RISK,
) -> PageScore:
    """`crawler.safety.check` and `score_page` in one call, for callers that need only the score."""
    from crawler.safety import check

    return score_page(texts, check(texts, terms), site_risk=site_risk)
