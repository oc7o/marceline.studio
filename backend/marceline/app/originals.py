"""The customer's own originals and what was found of them: the phase 1 read model.

`GET /api/v1/app/originals` answers "is my work leaked" for every original the organisation owns;
`GET /api/v1/app/originals/{id}` answers "where, and what is its status" for one of them.

Five things are load-bearing here and none of them may be worked around locally:

- **Scope.** Both views start at `ownership.owned_originals`. An original this organisation does
  not own is answered exactly like an original that does not exist: 404 with the same body. A 403
  would confirm the row exists, which is itself the leak.
- **One original per video.** The references of one file (a v0 and a v1 grid, a 1 and a 2 fps
  re-fingerprint) are one original under one public id; their findings are merged per page
  (`merge_findings`).
- **Public ids only** (issue #77, ADR 0033). An original is named by `OwnedOriginal.public_id`, a
  finding's page by `Page.public_id`. No primary key of a reference, page or organisation appears
  in a path or a payload; an integer in the path is just another unknown id.
- **Safety.** Every leak and match is filtered through `safety.visible_page_filter` — safety-clear,
  scored at or above the media gate, no term hit ever — so a flagged page is not listed *and not
  counted*: a count would say a hidden page exists.
- **No imagery.** Nothing here reads `index.Media`. A finding is site, URL, availability and dates.
- **Confirmed only** (operator decision, 2026-09-17). A customer never sees a finding we have not
  confirmed — not listed, not counted, no case page, no URL — because what we are still looking
  at, and how fast, is internal. `confirmed_findings_for` is the one filter every customer view
  and the customer-host priority of hoster lookups (`marceline/hosting.py`) read through;
  `findings_for` (everything visible, confirmed or not) is only its building block.
- **No confidence insight** (operator decision, 2026-09-17). The evidence a customer gets is the
  code `fingerprint_match` or `no_fingerprint_match` (`evidence_of`) — never a band, a confidence,
  a frame count or a distance.
- **Extent is a fact, not a filter** (issue #62). `extent_of` says whether a copy is the whole
  original, an excerpt, or not determined. Excerpts are findings like any other; a customer never
  learns why an extent is not determined (another original on the page stays unmentioned).

`findings_for` drops a match nobody has decided whose confidence band is `low`: fingerprint
noise. A decision always wins over that rule — a leak row of any status but rejected, and a
**confirmed** match row whatever its band: matching re-bands decided rows on every run, so a band
that drops to `low` must never take a confirmed copy away from its customer (`counts_as_match`).
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Status

from index.models import Page
from marceline.app.auth import customer_auth
from marceline.app.ownership import (
    OwnedOriginal,
    organisation_of,
    owned_original,
    owned_originals,
)
from marceline.app.safety import visible_page_filter
from marceline.app.schemas import (
    AppErrorOut,
    CustomerCopyExtent,
    CustomerEvidence,
    CustomerOriginalDetailOut,
    CustomerOriginalOut,
    FindingCountsOut,
    FindingOut,
    OriginalState,
)
from matching.copy_extent import CopyExtent, combine_extents, current_extent
from matching.fingerprint import KIND_PHOTOS
from matching.models import ReferenceLeak, ReferenceMatch

router = Router(tags=["app"], auth=customer_auth)

# The same answer for "does not exist" and "belongs to someone else". Never make them differ.
NOT_FOUND = "Original not found"
# Both decision tables spell a human's "yes, this is mine" the same way.
CONFIRMED = ReferenceLeak.Status.CONFIRMED


@dataclass(frozen=True)
class Finding:
    """One page carrying one original, as far as a customer is concerned.

    `detected_at` and `confirmed_at` come from the decision rows themselves (`created_at`, and
    `updated_at` for a decision) — the only dates that exist for them. `marceline/app/cases.py`
    turns them into the case's own stamps, which are written once and never move afterwards.

    **`confirmed_at` is only a first approximation, and it drifts later.** `match_references`
    re-saves every match row on every run, confirmed ones included, which moves `updated_at`; the
    decision tables record no decision date. So no customer date is ever read from here once a
    case exists: `CaseFinding.verified_at`, written by the first sync that sees the confirmation,
    is the stored observation and wins from then on (`cases.confirmed_on`). The limit that
    remains: a decision re-saved *before* any sync ran contributes its later `updated_at`, and so
    does a finding whose case row was deleted (hidden, then visible again) and written anew.
    Running `sync_cases` after each matching run keeps that window to one run.

    `matched` says only whether a fingerprint match row stands behind the finding
    (`counts_as_match`). Its band and frame counts are not carried at all: customers get no insight
    into confidence or processing (operator decision, 2026-09-17).
    """

    page: Page
    confirmed: bool
    matched: bool
    detected_at: datetime
    confirmed_at: datetime | None
    # The match row's copy extent (issue #62); `unknown` without a match that counts.
    extent: CopyExtent = CopyExtent.UNKNOWN


def counts_as_match(match: ReferenceMatch) -> bool:
    """A match row that is evidence: confirmed (any band), or undecided and not `low` noise.

    **This is not the customer filter, and reading it as one is a mistake two independent
    reviewers have now made** (issue #83, and again on 2026-09-18). It is permissive on purpose —
    it decides the `matched` flag and the extent, nothing else. What a customer may see goes
    through `confirmed_findings_for` / `confirmed_findings_of` below, which keep only rows whose
    `confirmed` is true, and `confirmed` is set from an explicit human `CONFIRMED` status
    (`decided` above), never from this function. An undecided review-band row is therefore never
    listed, never counted and never a case. Follow the call chain before concluding otherwise.

    A row a candidate generator proposed — the embedding (ADR 0037) or the metadata scorer
    (ADR 0040) — is **never** one of them, whatever a human then decided about the copy: it carries
    no frame evidence at all, so calling it a fingerprint match would tell a customer something
    untrue. Such a confirmed row is still a finding — a person judged the pictures — and its
    evidence reads `no_fingerprint_match`, which is exactly the case. Nothing about either signal
    reaches a customer schema: no confidence, no band, no distance, no candidate count.
    """
    if match.candidate_source in ReferenceMatch.CANDIDATE_ONLY_SOURCES:
        return False
    return match.status == CONFIRMED or match.band != ReferenceMatch.Band.LOW


def findings_for(reference_ids: Sequence[int]) -> dict[int, list[Finding]]:
    """Visible findings per original: human decisions and fingerprint matches, flagged pages out."""
    if not reference_ids:
        return {}
    leaks = {
        (row.reference_id, row.page_id): row
        for row in ReferenceLeak.objects.filter(reference_id__in=reference_ids)
        .exclude(status=ReferenceLeak.Status.REJECTED)
        .filter(visible_page_filter("page"))
        .select_related("page__site")
    }
    matches = {
        (row.reference_id, row.page_id): row
        for row in ReferenceMatch.objects.filter(reference_id__in=reference_ids)
        .exclude(status=ReferenceMatch.Status.REJECTED)
        .filter(visible_page_filter("page"))
        .select_related("page__site")
    }
    findings: dict[int, list[Finding]] = defaultdict(list)
    for reference_id, page_id in leaks.keys() | matches.keys():
        leak = leaks.get((reference_id, page_id))
        match = matches.get((reference_id, page_id))
        matched = match is not None and counts_as_match(match)
        # A confirmed second-signal candidate is a finding on the human's decision alone; an
        # undecided one is nothing at all, and `confirmed_findings_for` drops it anyway.
        confirmed_candidate = match is not None and match.status == CONFIRMED and not matched
        if leak is None and not matched and not confirmed_candidate:
            continue
        page = leak.page if leak is not None else (match.page if match is not None else None)
        if page is None:  # pragma: no cover - a key comes from one of the two dicts
            continue
        decisions: tuple[ReferenceLeak | ReferenceMatch, ...] = tuple(
            row for row in (leak, match) if row is not None
        )
        decided = [row.updated_at for row in decisions if row.status == CONFIRMED]
        findings[reference_id].append(
            Finding(
                page=page,
                confirmed=bool(decided),
                matched=matched,
                detected_at=min(row.created_at for row in decisions),
                confirmed_at=min(decided) if decided else None,
                extent=current_extent(match) if matched else CopyExtent.UNKNOWN,
            )
        )
    for rows in findings.values():
        rows.sort(key=lambda row: (not row.confirmed, row.page.pk))
    return findings


def confirmed_findings_for(reference_ids: Sequence[int]) -> dict[int, list[Finding]]:
    """What a customer may see: confirmed findings only, flagged pages already out.

    An original whose only findings are unconfirmed has no entry at all, so every customer view
    answers for it exactly as for an original nothing was found of.
    """
    confirmed = {
        reference_id: [row for row in rows if row.confirmed]
        for reference_id, rows in findings_for(reference_ids).items()
    }
    return {reference_id: rows for reference_id, rows in confirmed.items() if rows}


def merge_findings(rows: Iterable[Finding]) -> list[Finding]:
    """One finding per page across the references of one file.

    A page confirmed on any reference is confirmed, a page matched on any is matched, and every
    date is the earliest observation any reference made — the same page found on the v0 and the v1
    grid is one copy, first seen when either first saw it. The extent is the most cautious one any
    reference determined (`copy_extent.combine_extents`).
    """
    by_page: dict[int, list[Finding]] = defaultdict(list)
    for row in rows:
        by_page[row.page.pk].append(row)
    merged = []
    for same_page in by_page.values():
        confirmed = [row.confirmed_at for row in same_page if row.confirmed_at is not None]
        merged.append(
            Finding(
                page=same_page[0].page,
                confirmed=any(row.confirmed for row in same_page),
                matched=any(row.matched for row in same_page),
                detected_at=min(row.detected_at for row in same_page),
                confirmed_at=min(confirmed) if confirmed else None,
                extent=combine_extents([row.extent for row in same_page]),
            )
        )
    merged.sort(key=lambda row: (not row.confirmed, row.page.pk))
    return merged


def confirmed_findings_of(originals: Sequence[OwnedOriginal]) -> dict[int, list[Finding]]:
    """What a customer may see per original (keyed by the representative's id), members merged.

    Merged before the confirmed filter, so a page confirmed on one reference and still undecided
    on another is a confirmed copy. An original with nothing confirmed has no entry.
    """
    per_reference = findings_for([pk for original in originals for pk in original.member_ids])
    result: dict[int, list[Finding]] = {}
    for original in originals:
        rows = merge_findings(
            row for pk in original.member_ids for row in per_reference.get(pk, [])
        )
        confirmed = [row for row in rows if row.confirmed]
        if confirmed:
            result[original.pk] = confirmed
    return result


def state_of(findings: Sequence[Finding]) -> OriginalState:
    """The three states of the overview, from the (confirmed) findings a customer may see."""
    if not findings:
        return "nothing_found"
    if all(row.page.availability == Page.Availability.REMOVED for row in findings):
        return "found_removed"
    return "found_online"


def evidence_of(finding: Finding) -> CustomerEvidence:
    """What "why we think this is yours" may say: that the fingerprints match, or that they don't.

    `fingerprint_match` when a match row counts as evidence (`counts_as_match`); a copy confirmed
    by a leak decision on top of undecided `low` noise, or with no match row, was confirmed without
    one. How sure the match is, and how many frames matched, stay internal.
    """
    return "fingerprint_match" if finding.matched else "no_fingerprint_match"


def extent_of(finding: Finding) -> CustomerCopyExtent:
    """How much of the original this copy carries: the whole video, a part, or not determined.

    A compilation candidate is `not_determined` for a customer: naming it would say another
    original is on the page, and whether it is a compilation is for a person to judge.
    """
    if finding.extent == CopyExtent.FULL_COPY:
        return "full_copy"
    if finding.extent == CopyExtent.EXCERPT:
        return "excerpt"
    return "not_determined"


def counts_of(findings: Sequence[Finding]) -> FindingCountsOut:
    return FindingCountsOut(
        confirmed=len(findings),
        removed=sum(1 for row in findings if row.page.availability == Page.Availability.REMOVED),
    )


def original_out(original: OwnedOriginal, findings: Sequence[Finding]) -> CustomerOriginalOut:
    reference = original.representative
    return CustomerOriginalOut(
        id=original.public_id,
        title=reference.title,
        # Every reference of one file shares its kind — a collection digest is domain-separated
        # from a file's sha256 (ADR 0020), so the two shapes can never be grouped together.
        # `Reference.kind` is a CharField, so the narrowing is explicit rather than a cast: an
        # unknown value reads as a video, which is what every original was before issue #116.
        # The two spellings are the same strings `matching.fingerprint` defines, pinned by
        # `test_app_fingerprints.test_the_customer_kinds_are_the_matching_kinds`.
        kind="photos" if reference.kind == KIND_PHOTOS else "video",
        duration_s=reference.duration_s,
        created_at=reference.created_at,
        state=state_of(findings),
        counts=counts_of(findings),
    )


def finding_out(finding: Finding) -> FindingOut:
    page = finding.page
    return FindingOut(
        page_id=page.public_id,
        site=page.site.domain,
        url=page.url,
        availability=page.availability,
        last_checked=page.availability_checked_at,
        evidence=evidence_of(finding),
        extent=extent_of(finding),
    )


@router.get("", response=list[CustomerOriginalOut])
def originals(request: HttpRequest) -> list[CustomerOriginalOut]:
    """Every original this organisation owns, one per video, newest first. No one else's."""
    originals = owned_originals(organisation_of(request.user))
    findings = confirmed_findings_of(originals)
    return [original_out(original, findings.get(original.pk, [])) for original in originals]


@router.get("/{original_id}", response={200: CustomerOriginalDetailOut, 404: AppErrorOut})
def original_detail(
    request: HttpRequest, original_id: str
) -> Status[CustomerOriginalDetailOut | AppErrorOut]:
    """One original with every human-reviewed matching copy, by its public id.

    404 unless this organisation owns it — the same body for a malformed, unknown or foreign id.
    """
    original = owned_original(organisation_of(request.user), original_id)
    if original is None:
        return Status(404, AppErrorOut(detail=NOT_FOUND))
    findings = confirmed_findings_of([original]).get(original.pk, [])
    return Status(
        200,
        CustomerOriginalDetailOut(
            original=original_out(original, findings),
            findings=[finding_out(row) for row in findings],
        ),
    )
