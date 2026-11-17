"""Ground-truth suggestion persistence and read-only candidate preview."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from index.models import Page
from matching.creators import creator_ids, creators_of
from matching.leak_scoring import CLUSTER_WEIGHT, MIN_SCORE, LeakScore, score_page
from matching.models import ClusterMember, Reference, ReferenceLeak, ReferenceMatch
from matching.services import mirror_groups


@dataclass(frozen=True)
class Suggestion:
    page: Page
    evidence: LeakScore


def rank_candidates(reference: Reference) -> list[Suggestion]:
    """Read-only ranking, before excluding reviewed candidates or applying the limit."""
    # Every performer counts (ADR 0010, 2026-09-16): their names are stripped from title tokens,
    # and a link to any of them satisfies the creator link a duration-only candidate needs.
    names = [
        name
        for creator in creators_of(reference)
        for name in (creator.name, *(str(alias) for alias in creator.aliases))
    ]
    linked_ids = creator_ids(reference)
    matches = dict(reference.matches.values_list("page_id", "strength"))
    related = related_evidence(reference)
    ranked = []
    pages = Page.objects.filter(active=True)  # inactive pages are never suggested (issue #71)
    for page in pages.select_related("site").prefetch_related("creators").order_by("pk"):
        linked = any(creator.pk in linked_ids for creator in page.creators.all())
        evidence = score_page(
            reference.title,
            reference.duration_s,
            page.title,
            page.raw_extraction,
            names,
            linked,
            matches.get(page.pk),
            *related.get(page.pk, (None, None)),
        )
        if evidence.score >= MIN_SCORE:
            ranked.append(Suggestion(page, evidence))
    return sorted(ranked, key=lambda item: (-item.evidence.score, item.page.pk))


def suggest_leaks(
    references: QuerySet[Reference], *, limit_per_reference: int = 5, dry_run: bool = False
) -> list[str]:
    if limit_per_reference < 1:
        raise ValueError("limit-per-reference must be positive")
    lines = []
    for reference in references:
        if dry_run:
            selected = _select(reference, rank_candidates(reference), limit_per_reference)
        else:
            with transaction.atomic():
                Reference.objects.select_for_update().get(pk=reference.pk)
                selected = _select(reference, rank_candidates(reference), limit_per_reference)
                for item in selected:
                    row, _ = ReferenceLeak.objects.get_or_create(
                        reference=reference,
                        page=item.page,
                        defaults={"source": ReferenceLeak.Source.SUGGESTED},
                    )
                    ReferenceLeak.objects.filter(pk=row.pk, status="proposed").update(
                        source=(
                            "cluster" if item.evidence.signals["related_kind"] else "suggested"
                        ),
                        score=item.evidence.score,
                        signals=item.evidence.signals,
                        updated_at=timezone.now(),
                    )
                ReferenceLeak.objects.filter(reference=reference, status="proposed").exclude(
                    page_id__in=[item.page.pk for item in selected]
                ).delete()
        lines.append(
            f"Reference {reference.pk} {reference.title}: {len(selected)} suggestions"
            + (" (dry run)" if dry_run else "")
        )
    return lines


def _select(reference: Reference, candidates: Iterable[Suggestion], limit: int) -> list[Suggestion]:
    """Evidence-backed candidates (a ReferenceMatch, cluster or mirror relation) are never
    capped; only duration-only candidates are limited to `limit` per reference."""
    decided = set(reference.leaks.exclude(status="proposed").values_list("page_id", flat=True))
    decided.update(
        ReferenceMatch.objects.filter(reference=reference)
        .exclude(status="proposed")
        .values_list("page_id", flat=True)
    )
    selected: list[Suggestion] = []
    duration_only = 0
    for item in candidates:
        if item.page.pk in decided:
            continue
        if has_evidence(item.evidence):
            selected.append(item)
        elif duration_only < limit:
            duration_only += 1
            selected.append(item)
    return selected


def has_evidence(evidence: LeakScore) -> bool:
    signals = evidence.signals
    return bool(signals.get("matched_strength") or signals.get("related_kind"))


def active_cluster_pages() -> list[set[int]]:
    """Active pages per cluster, rejected memberships left out.

    Inactive pages (issue #71, ADR 0029) are never evidence, so they neither seed nor receive
    cluster propagation or corroboration, exactly like `mirror_groups`.
    """
    groups: dict[int, set[int]] = {}
    members = ClusterMember.objects.exclude(status="rejected").filter(page__active=True)
    for cluster, page in members.values_list("cluster_id", "page_id"):
        groups.setdefault(cluster, set()).add(page)
    return list(groups.values())


def related_pages(
    memberships: Sequence[tuple[str, Sequence[set[int]]]], strong: set[int]
) -> dict[int, tuple[int, str]]:
    """Pure: page -> (seed, kind) for pages sharing a group with a strong seed (not itself)."""
    related: dict[int, tuple[int, str]] = {}
    for kind, groups in memberships:
        for pages in groups:
            for seed in sorted(pages & strong):
                for page in sorted(pages - {seed}):
                    related.setdefault(page, (seed, kind))
    return related


def related_evidence(
    reference: Reference, current_strong: set[int] | None = None
) -> dict[int, tuple[int, str]]:
    """Use current memberships; rejected matches/leaks cannot seed propagation."""
    strong = set(
        reference.matches.filter(strength="strong")
        .exclude(status="rejected")
        .values_list("page_id", flat=True)
    )
    strong -= set(reference.leaks.filter(status="rejected").values_list("page_id", flat=True))
    if current_strong is not None:
        strong &= current_strong
    return related_pages([("cluster", active_cluster_pages()), ("mirror", mirror_groups())], strong)


def propagate_cluster_leaks(reference: Reference, current_strong: set[int]) -> None:
    """Called under the matching reference lock; refresh all supported proposals."""
    related = related_evidence(reference, current_strong)
    decided = set(reference.leaks.exclude(status="proposed").values_list("page_id", flat=True))
    decided.update(reference.matches.exclude(status="proposed").values_list("page_id", flat=True))
    for page_id, (seed, kind) in related.items():
        if page_id in decided:
            continue
        ReferenceLeak.objects.update_or_create(
            reference=reference,
            page_id=page_id,
            defaults={
                "source": "cluster",
                "score": CLUSTER_WEIGHT,
                "signals": {"matched_page_id": str(seed), "related_kind": kind},
            },
        )
    reference.leaks.filter(status="proposed", source="cluster").exclude(
        page_id__in=related.keys() - decided
    ).delete()
