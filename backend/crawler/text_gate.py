"""The one refusal applied before any text leaves this server (ADR 0013 and ADR 0042 amendments).

`discover_sources` has refused a *planned search query* since ADR 0013's amendment: a query that
hits the safety terms, the minor near misses or the German non-consent wording is never sent to
the search API. That refusal is in code, before any call, and it is the comparator for every new
third-party call.

ADR 0042's adapter repair sends something far larger — up to `llm.MAX_INPUT_CHARS` characters of
an indexed page's *own* text — to an LLM broker, and on the one page shape that triggers it (a
blind parse: no page, no title or no media) **nothing has judged that text yet**: the terms, the
score and the media gate run afterwards, in `crawler.services.store_parse_result`. The
review of PR #112 (2026-09-18) blocked the first production promotion over exactly that.

So both paths now ask the same question here, over the same normalized text and the same
`catalog.SafetyTerm` rows: **is there anything about this text that means it must not leave?**

Two halves, deliberately separated:

- `judge` is pure — text and compiled terms in, rule names out. It never quotes the text it
  judged: a caller whose text is *ours* (a query we composed) may print the matched word, a
  caller whose text is a *page's* may not, and `TextVerdict` keeps the two apart.
- `transmission_refusal` is the database-aware gate the repair path calls. It reads the terms
  fresh (never the hot-path cache: a control is worth a query), judges the exact string that
  would be sent, and adds the two facts the text cannot show — the site is disabled, or this URL
  is already flagged from an earlier crawl.

**Fail closed is the caller's job and it is not optional**: `crawler.llm.extract_page_fields`
treats any exception from here as a refusal.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from django.conf import settings

from crawler import safety, safety_score
from crawler.models import CrawlTask
from crawler.services import load_safety_terms, normalize_url, site_risk
from crawler.sites.common import ascii_fold
from index.models import Page

#: Prefix of every refusal reason recorded for a call that was **not** made. `crawler.repair`
#: excludes such rows from the per-site daily cap: a refusal costs nothing, so it may not spend
#: anything either.
REFUSED = "refused_"
REFUSED_SAFETY = f"{REFUSED}safety"
REFUSED_SITE_DISABLED = f"{REFUSED}site_disabled"
REFUSED_PAGE_FLAGGED = f"{REFUSED}page_flagged"
REFUSED_CHECK_FAILED = f"{REFUSED}check_failed"

# Never let wording that points towards minors or non-consensual content leave the server, not
# even as a synonym. The safety terms and the media gate's
# near-miss signals cover most of it; these cover German non-consent wording they do not.
# Moved here from `crawler.discovery.services` on 2026-09-18 so both callers share one list.
FORBIDDEN_WORDS = (
    "heimlich",
    "versteckt",
    "versteckte kamera",
    "ex freundin",
    "exfreundin",
    "spanner",
    "ko tropfen",
    "k o tropfen",
)
#: Score signals that mean "do not send this": a term hit of either action, an explicit under-age
#: pattern, or minor-context wording. The remaining signals (thin or uncovered text, a bare low
#: number, the site's own risk) say how little we know, not that something is wrong; they are
#: caught by `min_score` where a caller asks for it.
UNSAFE_SIGNALS = frozenset(
    {
        safety_score.BLOCK_TERM,
        safety_score.REVIEW_TERM,
        safety_score.AGE_PATTERN,
        safety_score.MINOR_CONTEXT,
    }
)
FORBIDDEN_WORD = "forbidden_word"
LOW_SCORE = "low_score"
_FORBIDDEN = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(word) for word in FORBIDDEN_WORDS) + r")(?!\w)"
)


@dataclass(frozen=True)
class TextVerdict:
    """Why a text must not be sent, named by rule and never by content.

    `rules` holds rule names only — the score signals (`block_term`, `review_term`,
    `age_pattern`, `minor_context:school`) plus `forbidden_word` and `low_score`. `categories`
    holds the categories of the `catalog.SafetyTerm` rows that fired, the same identifiers safety
    mail is allowed to carry (`crawler/alerts.py`). Neither is ever a word out of the text.

    `word` is the matched non-consent word and is **only** for a caller whose text is our own
    (a search query we composed). A page's text is content: never put `word` in a log, a database
    row, a report or a commit message.
    """

    rules: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    word: str = ""

    @property
    def unsafe(self) -> bool:
        return bool(self.rules)

    def reason(self, outcome: str = REFUSED_SAFETY) -> str:
        """The recordable one-liner: the outcome and the rule names, never the text."""
        named = ", ".join([*self.rules, *self.categories])
        return f"{outcome}: {named}" if named else outcome


def judge(
    text: str,
    terms: Sequence[safety.CompiledTerm],
    *,
    site_risk: safety_score.SiteRisk = safety_score.NO_SITE_RISK,
    min_score: float | None = None,
) -> TextVerdict:
    """Is there anything about `text` that means it must not leave the server? Pure.

    `min_score` applies the media gate's own bar (ADR 0018) to the text as well, for a caller that
    has one: text we would not fetch a picture for is text we do not hand to a third party. A
    caller that passes `None` is judged on terms, near misses and non-consent wording alone, which
    is what `discover_sources` has always done.
    """
    # The near-miss patterns spell German words without umlauts ("schuler…"), so check both forms.
    variants = list(dict.fromkeys([text, ascii_fold(text)]))
    word = ""
    for variant in variants:
        if match := _FORBIDDEN.search(safety.normalize(variant)):
            word = match.group(0)
            break
    verdict = safety.check(variants, terms)
    score = safety_score.score_page(variants, verdict, site_risk=site_risk)
    rules = {signal for signal in score.signals if signal.split(":")[0] in UNSAFE_SIGNALS}
    if word:
        rules.add(FORBIDDEN_WORD)
    if min_score is not None and score.score < min_score:
        rules.add(LOW_SCORE)
    categories = {term.category for term in verdict.matches if term.category}
    return TextVerdict(tuple(sorted(rules)), tuple(sorted(categories)), word)


def transmission_refusal(task: CrawlTask, page_text: str) -> str:
    """Why this exact page text must not be sent to the LLM provider, or "" if it may.

    Reads the database. Called as the first statement of `crawler.llm.extract_page_fields`, i.e.
    before the request is built, before the `LLMCall` row exists and before any client is opened,
    so nothing can leave ahead of it.

    Three refusals, in the order that costs least:

    1. **The site is not enabled.** A site we have stopped crawling (an
       operator decision) is not a site whose pages we pay to have read for us.
    2. **This URL is already flagged** from an earlier crawl. The text in hand has not been judged
       yet, but the *page* has, and a page under review or blocked is off-limits for everything —
       a repair would be a fresh transmission of text we already know is not clear.
    3. **The text itself**, against today's terms (loaded fresh, never the hot-path cache), the
       minor near misses, the German non-consent wording and the media gate's own score bar.

    There is no per-site switch for the repair: `CRAWLER_LLM_REPAIR_ENABLED`, the *recorded*
    breakage verdict and `Site.enabled` are the switches (ADR 0042 amendment 2026-09-18).
    """
    if not task.site.enabled:
        return REFUSED_SITE_DISABLED
    existing = (
        Page.objects.filter(url=normalize_url(task.url))
        .exclude(safety_status=Page.SafetyStatus.CLEAR)
        .values_list("safety_status", "safety_matches")
        .first()
    )
    if existing is not None:
        status, matches = existing
        categories = sorted(
            {str(match.get("category", "")) for match in matches if match.get("category")}
        )
        return f"{REFUSED_PAGE_FLAGGED}: {', '.join([str(status), *categories])}"
    text_verdict = judge(
        page_text,
        load_safety_terms(),
        site_risk=site_risk(task.site),
        min_score=float(settings.SAFETY_GATE_MIN_SCORE),
    )
    return text_verdict.reason() if text_verdict.unsafe else ""
