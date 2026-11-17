"""Pure review ranking; tight duration gating only for candidates without evidence."""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from matching.duration import page_duration

EXACT_DURATION_MAX_DIFF_S = 1.5
NEAR_DURATION_WEIGHT = 0.50
STRONG_MATCH_WEIGHT = 1.0
WEAK_MATCH_WEIGHT = 0.9
CLUSTER_WEIGHT = 0.8
MIN_SCORE = 0.50
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "with",
        "for",
        "from",
        "by",
        "is",
        "my",
        "new",
        "fanhub",
        "leaked",
        "leak",
        "hd",
        "4k",
        "2160p",
        "1080p",
        "720p",
        "full",
        "video",
        "videos",
        "watch",
        "free",
        "site01",
        "site02",
        "site03",
        "site04",
    ]
)


def title_tokens(title: str, names: Iterable[str]) -> set[str]:
    excluded = STOPWORDS | {
        token for name in names for token in re.findall(r"[a-z0-9]+", name.lower())
    }
    return {token for token in re.findall(r"[a-z0-9]+", title.lower()) if token not in excluded}


@dataclass(frozen=True)
class LeakScore:
    score: float
    signals: dict[str, float | bool | str | None]


def score_page(
    reference_title: str,
    reference_duration: float | None,
    page_title: str,
    raw_extraction: object,
    names: Iterable[str],
    creator_linked: bool,
    matched_strength: str | None,
    related_page_id: int | None = None,
    related_kind: str | None = None,
) -> LeakScore:
    names = tuple(names)
    first, second = title_tokens(reference_title, names), title_tokens(page_title, names)
    similarity = len(first & second) / len(first | second) if first | second else 0.0
    duration = page_duration(raw_extraction)
    original = page_duration({"duration_s": reference_duration})
    diff = duration - original if duration is not None and original is not None else None
    ratio = duration / original if duration is not None and original is not None else None
    duration_score = 0.0
    if diff is not None and creator_linked and abs(diff) <= EXACT_DURATION_MAX_DIFF_S:
        duration_score = NEAR_DURATION_WEIGHT
    match_score = {"strong": STRONG_MATCH_WEIGHT, "weak": WEAK_MATCH_WEIGHT}.get(
        matched_strength or "", 0.0
    )
    return LeakScore(
        max(duration_score, match_score, CLUSTER_WEIGHT if related_page_id is not None else 0.0),
        {
            "duration_diff_s": diff,
            "duration_ratio": ratio,
            "title_similarity": similarity,
            "creator_linked": creator_linked,
            "matched_strength": matched_strength,
            "matched_page_id": str(related_page_id) if related_page_id is not None else None,
            "related_kind": related_kind,
        },
    )
