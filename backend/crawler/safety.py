"""Content-safety text checks: pure functions, no database (DB workflows live in services).

Terms come from `catalog.SafetyTerm`. Page text is lowercased and punctuation is collapsed into
single spaces, so "Under_Age", "under-age" and "under age" all read "under age". Plain terms
match whole words; regex terms run against the same normalized text, bounded by word edges.
See docs/safety.md for what operators do with flagged pages.
"""

import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from catalog import safety_rules
from index.models import Page

logger = logging.getLogger(__name__)

CLEAR = Page.SafetyStatus.CLEAR
REVIEW = Page.SafetyStatus.REVIEW
BLOCKED = Page.SafetyStatus.BLOCKED
_SEPARATORS = re.compile(r"[\W_]+")
MAX_TEXT_DEPTH = 4


@dataclass(frozen=True)
class Term:
    term: str
    category: str
    action: str
    is_regex: bool = False


@dataclass(frozen=True)
class CompiledTerm:
    term: Term
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Verdict:
    status: Page.SafetyStatus
    matches: tuple[Term, ...] = ()

    @property
    def categories(self) -> list[str]:
        return sorted({term.category for term in self.matches})

    @property
    def incident_due(self) -> bool:
        return self.status == BLOCKED and safety_rules.incident_due(matches_json(self.matches))

    @property
    def release_queue_only(self) -> bool:
        return safety_rules.release_queue_only(matches_json(self.matches))


def normalize(text: str) -> str:
    """Casefold, NFKC, and collapse everything that is not a letter or digit into one space."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_SEPARATORS.sub(" ", folded).split())


def compile_terms(terms: Iterable[Term]) -> list[CompiledTerm]:
    """Compile enabled terms; invalid regexes are logged and left out, never raised."""
    compiled: list[CompiledTerm] = []
    for term in terms:
        body = term.term if term.is_regex else re.escape(normalize(term.term))
        if not body:
            continue
        try:
            pattern = re.compile(rf"(?<!\w)(?:{body})(?!\w)")
        except re.error:
            logger.error("invalid safety term regex skipped: %r", term.term)
            continue
        compiled.append(CompiledTerm(term, pattern))
    return compiled


def collect_texts(value: object, depth: int = 0) -> list[str]:
    """All strings in a JSON-like value (strings, lists, dicts); keys are not included."""
    if isinstance(value, str):
        return [value]
    if depth >= MAX_TEXT_DEPTH:
        return []
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in collect_texts(item, depth + 1)]
    if isinstance(value, list | tuple):
        return [text for item in value for text in collect_texts(item, depth + 1)]
    return []


def rule_version(terms: Iterable[CompiledTerm]) -> str:
    """The version id of the rule set these compiled terms came from (`catalog.safety_rules`)."""
    return safety_rules.rule_version(
        (c.term.term, c.term.is_regex, c.term.category, c.term.action) for c in terms
    )


def check(texts: Iterable[str], terms: Sequence[CompiledTerm]) -> Verdict:
    """Blocked if any blocking term matches, review if only review terms match, else clear.

    Every action except `review` blocks: `auto_block`, `auto_block_and_incident` and
    `customer_release_queue` all block at once, with no review step (catalog/safety_rules.py).
    """
    haystack = "\n".join(normalize(text) for text in texts if text)
    matches = tuple(c.term for c in terms if haystack and c.pattern.search(haystack))
    if any(safety_rules.blocks(term.action) for term in matches):
        return Verdict(BLOCKED, matches)
    if matches:
        return Verdict(REVIEW, matches)
    return Verdict(CLEAR)


def matches_json(matches: Iterable[Term]) -> list[dict[str, str]]:
    return [{"term": t.term, "category": t.category, "action": t.action} for t in matches]


@dataclass(frozen=True)
class Resolution:
    status: Page.SafetyStatus
    matches: list[dict[str, str]]
    keep_review: bool  # a human decision still covers these matches


def resolve(
    current_status: str | None,
    current_matches: Sequence[Mapping[str, object]],
    reviewed: bool,
    verdict: Verdict,
) -> Resolution:
    """The status a page should get for a fresh verdict.

    - New pages take the verdict.
    - A human decision stands while the page matches no term beyond those it was reviewed with,
      and no term's action became stricter since (a page cleared while "rape" was a review term
      is blocked once "rape" is an auto-block term — the human never judged that rule).
    - Blocked pages are never unblocked automatically; only a reviewer does that.
    - Otherwise the verdict applies (review pages whose terms stopped matching become clear).
    """
    if current_status is None:
        return Resolution(verdict.status, matches_json(verdict.matches), keep_review=False)
    known: dict[str, int] = {}
    for match in current_matches:
        term = str(match.get("term"))
        known[term] = max(known.get(term, -1), safety_rules.rank(match.get("action")))
    status = Page.SafetyStatus(current_status)
    covered = all(
        term.term in known and safety_rules.rank(term.action) <= known[term.term]
        for term in verdict.matches
    )
    if reviewed and covered:
        return Resolution(status, list(map(dict_str, current_matches)), keep_review=True)
    if status == BLOCKED and verdict.status != BLOCKED:
        return Resolution(status, list(map(dict_str, current_matches)), keep_review=False)
    return Resolution(verdict.status, matches_json(verdict.matches), keep_review=False)


def dict_str(match: Mapping[str, object]) -> dict[str, str]:
    return {str(key): str(value) for key, value in match.items()}
