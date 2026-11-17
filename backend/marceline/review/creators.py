"""Staff review queue for `catalog.Creator.needs_review` rows (issue #29 follow-up, operator
decision 2026-09-17).

A needs_review creator is a name a customer typed when adding an original in the app that matched
no existing creator by exact name (`marceline/app/fingerprints.py`); it stays out of crawling,
page linking and discovery until a human resolves it here (`catalog.models.Creator` docstring):

- **Mark reviewed**: it is a genuinely new creator; `needs_review` is cleared and it becomes a
  normal, crawlable creator.
- **Merge into an existing creator**: it is a duplicate identity (a customer typed a name with
  different spacing/case/punctuation that our exact-name check missed, or simply retyped a
  performer who already exists). Every reference cast link and page link the source creator
  picked up moves to the target, the source's own aliases join the target's, and the source row
  is deleted.
- **Add as alias of an existing creator**: the typed name is not a person but a studio/brand name
  (aliases are never matched automatically — a customer typing a studio must not silently become
  some performer's claim, see `fingerprints.customer_cast`). The move is the same as a merge, and
  additionally the source's own name joins the target's aliases, so a future human recognises it.

Both moving actions are logged (`marceline.CreatorMergeLog`) before the source row is deleted —
this is the only place its name and id survive. Neither runs from a queue read; both need an
explicit `target_id` and a confirmation click in the SPA. Nothing here is automatic.
"""

import difflib
import re

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest
from ninja import Router, Status

from accounts.models import Organisation
from catalog.models import Creator
from catalog.suppression import is_suppressed
from index.models import Page
from marceline.models import CreatorMergeLog, OriginalOwnership
from marceline.review.auth import staff_auth
from marceline.review.common import MAX_LIMIT
from marceline.review.schemas import (
    CreatorMarkReviewedOut,
    CreatorMergeIn,
    CreatorMergeOut,
    CreatorReviewItemOut,
    CreatorReviewListOut,
    ErrorOut,
    SimilarCreatorOut,
)
from matching.creators import set_creators
from matching.models import Reference

router = Router(tags=["review"], auth=staff_auth)

#: Below this, two names are treated as unrelated; a "simple normalised similarity" (operator
#: 2026-09-17) suggestion, never applied automatically.
SIMILARITY_THRESHOLD = 0.6
MAX_SUGGESTIONS = 5
NOT_FOUND = ErrorOut(detail="Unknown creator")


def normalised_name(name: str) -> str:
    """Lowercase, punctuation collapsed to single spaces: cheap enough to compare every creator."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _names(creator: Creator) -> list[str]:
    aliases = creator.aliases if isinstance(creator.aliases, list) else []
    return [creator.name, *aliases]


def similar_creators(creator: Creator, limit: int = MAX_SUGGESTIONS) -> list[SimilarCreatorOut]:
    """Existing creators whose name or an alias reads close to this one's name. Suggestions only."""
    target = normalised_name(creator.name)
    scored: list[tuple[float, Creator]] = []
    for candidate in Creator.objects.exclude(pk=creator.pk):
        best = max(
            (
                difflib.SequenceMatcher(None, target, normalised_name(name)).ratio()
                for name in _names(candidate)
            ),
            default=0.0,
        )
        if best >= SIMILARITY_THRESHOLD:
            scored.append((best, candidate))
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [
        SimilarCreatorOut(
            id=c.pk, name=c.name, aliases=list(c.aliases or []), similarity=round(s, 2)
        )
        for s, c in scored[:limit]
    ]


def _reference_ids(creator: Creator) -> set[int]:
    return set(
        Reference.objects.filter(Q(creator_id=creator.pk) | Q(creators=creator.pk)).values_list(
            "pk", flat=True
        )
    )


def creator_organisations(creator: Creator) -> list[str]:
    """Organisation(s) whose customer upload created or reused this creator's name."""
    reference_ids = _reference_ids(creator)
    if not reference_ids:
        return []
    return list(
        Organisation.objects.filter(
            owned_originals__reference_id__in=reference_ids,
            owned_originals__assigned_via=OriginalOwnership.Via.CUSTOMER,
        )
        .distinct()
        .order_by("name")
        .values_list("name", flat=True)
    )


def _item(creator: Creator) -> CreatorReviewItemOut:
    return CreatorReviewItemOut(
        id=creator.pk,
        name=creator.name,
        created_at=creator.created_at,
        organisations=creator_organisations(creator),
        linked_references=len(_reference_ids(creator)),
        similar=similar_creators(creator),
    )


class MergeRefused(Exception):
    """A merge/alias a staff confirmation dialog should catch as a 409, not a 500."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def merge_creator(
    source: Creator, target: Creator, *, by: User | None, as_alias: bool
) -> CreatorMergeLog:
    """Move `source`'s reference cast links, page links and aliases into `target`, then delete it.

    Raises `MergeRefused` for anything a staff confirmation dialog should catch instead of a
    500: merging into itself, a source that is not (or is no longer) `needs_review`, or either
    side being under an index suppression.

    **Suppression blocks a merge in both directions** (docs/suppression.md). Merging *into* a
    suppressed creator would try to move page links onto a name that must have none; merging a
    suppressed name *away* would delete its marker (the FK is `PROTECT`, so it would fail
    mid-transaction anyway) and, with `as_alias`, would copy the suppressed name into another
    creator's aliases — which is how the crawler would start linking it again.
    """
    if source.pk == target.pk:
        raise MergeRefused("A creator cannot be merged into itself.")
    if not source.needs_review:
        raise MergeRefused("Only a needs_review creator can be merged or aliased this way.")
    if suppressed := [creator.name for creator in (source, target) if is_suppressed(creator)]:
        raise MergeRefused(
            "Suppressed name, no merge or alias (docs/suppression.md): " + ", ".join(suppressed)
        )
    with transaction.atomic():
        primary_refs = list(
            Reference.objects.filter(creator_id=source.pk).prefetch_related("creators")
        )
        primary_ids = {reference.pk for reference in primary_refs}
        for reference in primary_refs:
            others = [c for c in reference.creators.all() if c.pk not in (source.pk, target.pk)]
            reference.creator = target
            reference.save(update_fields=["creator"])
            set_creators(reference, [*others, target])

        co_performer_refs = list(
            Reference.objects.filter(creators=source.pk)
            .exclude(pk__in=primary_ids)
            .prefetch_related("creators")
        )
        for reference in co_performer_refs:
            others = [c for c in reference.creators.all() if c.pk != source.pk]
            set_creators(reference, [*others, target])

        moved_pages = 0
        for page in Page.objects.filter(creators=source.pk).distinct():
            page.creators.remove(source)
            page.creators.add(target)
            moved_pages += 1

        source_aliases = source.aliases if isinstance(source.aliases, list) else []
        candidates = [*source_aliases, *([source.name] if as_alias else [])]
        target_aliases = list(target.aliases) if isinstance(target.aliases, list) else []
        added = [name for name in candidates if name != target.name and name not in target_aliases]
        if added:
            target.aliases = [*target_aliases, *added]
            target.save(update_fields=["aliases"])

        log = CreatorMergeLog.objects.create(
            source_creator_id=source.pk,
            source_name=source.name,
            target=target,
            as_alias=as_alias,
            moved_references=len(primary_ids) + len(co_performer_refs),
            moved_pages=moved_pages,
            aliases_added=added,
            merged_by=by if isinstance(by, User) else None,
        )
        source.delete()
    return log


def needs_review_count() -> int:
    """For the navigation badge (`GET /review/counts`)."""
    return Creator.objects.filter(needs_review=True).count()


@router.get("", response={200: CreatorReviewListOut})
def creator_review_queue(
    request: HttpRequest, limit: int = MAX_LIMIT
) -> Status[CreatorReviewListOut]:
    """`needs_review` creators, newest first, each with suggestions for a possible duplicate."""
    limit = MAX_LIMIT if limit < 1 else min(limit, MAX_LIMIT)
    queryset = Creator.objects.filter(needs_review=True).order_by("-created_at", "-pk")
    rows = list(queryset[: limit + 1])
    page = rows[:limit]
    return Status(
        200, CreatorReviewListOut(items=[_item(c) for c in page], truncated=len(rows) > limit)
    )


@router.post("/{creator_id}/mark-reviewed", response={200: CreatorMarkReviewedOut, 404: ErrorOut})
def mark_reviewed(
    request: HttpRequest, creator_id: int
) -> Status[CreatorMarkReviewedOut | ErrorOut]:
    """It is a genuinely new creator: becomes crawlable like any other (admin's "Mark reviewed")."""
    creator = Creator.objects.filter(pk=creator_id).first()
    if creator is None:
        return Status(404, NOT_FOUND)
    if creator.needs_review:
        creator.needs_review = False
        creator.save(update_fields=["needs_review"])
    return Status(200, CreatorMarkReviewedOut(id=creator.pk, needs_review=creator.needs_review))


def _merge_response(
    request: HttpRequest, creator_id: int, payload: CreatorMergeIn, *, as_alias: bool
) -> Status[CreatorMergeOut | ErrorOut]:
    source = Creator.objects.filter(pk=creator_id).first()
    target = Creator.objects.filter(pk=payload.target_id).first()
    if source is None or target is None:
        return Status(404, NOT_FOUND)
    user = request.user if isinstance(request.user, User) else None
    try:
        log = merge_creator(source, target, by=user, as_alias=as_alias)
    except MergeRefused as refused:
        return Status(409, ErrorOut(detail=refused.detail))
    return Status(
        200,
        CreatorMergeOut(
            source_id=log.source_creator_id,
            source_name=log.source_name,
            target_id=target.pk,
            target_name=target.name,
            as_alias=log.as_alias,
            moved_references=log.moved_references,
            moved_pages=log.moved_pages,
            aliases_added=list(log.aliases_added or []),
        ),
    )


@router.post("/{creator_id}/merge", response={200: CreatorMergeOut, 404: ErrorOut, 409: ErrorOut})
def merge(
    request: HttpRequest, creator_id: int, payload: CreatorMergeIn
) -> Status[CreatorMergeOut | ErrorOut]:
    """Merge a duplicate identity into an existing creator: never automatic, confirmation step."""
    return _merge_response(request, creator_id, payload, as_alias=False)


@router.post("/{creator_id}/alias", response={200: CreatorMergeOut, 404: ErrorOut, 409: ErrorOut})
def alias(
    request: HttpRequest, creator_id: int, payload: CreatorMergeIn
) -> Status[CreatorMergeOut | ErrorOut]:
    """The typed name is a studio/brand, not a person: record it as an alias of the target."""
    return _merge_response(request, creator_id, payload, as_alias=True)
