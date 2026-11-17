"""Typed request/response schemas of the review API."""

from datetime import datetime
from typing import Literal

from ninja import Field, Schema

Decision = Literal["confirm", "reject", "reset"]
MAX_BULK_IDS = 500


class ErrorOut(Schema):
    detail: str


class ConflictItemOut(Schema):
    kind: str
    id: int
    status: str
    reason: str


class ConflictOut(Schema):
    detail: str
    conflicts: list[ConflictItemOut] = Field(default_factory=list)


class DecisionIn(Schema):
    ids: list[int] = Field(min_length=1, max_length=MAX_BULK_IDS)
    decision: Decision


class IdsIn(Schema):
    ids: list[int] = Field(min_length=1, max_length=MAX_BULK_IDS)


class MediaRefOut(Schema):
    """An `index.Media` row usable with `GET /media/{id}/image`. Never includes the upstream URL."""

    id: int
    kind: str


class PageOut(Schema):
    id: int
    site: str
    title: str
    url: str
    duration_s: float | None
    availability: str = Field(description="playable / removed / unknown.")
    availability_checked_at: datetime | None = Field(
        description="Last availability check by the crawler; null = never checked."
    )
    uploaded_at: datetime | None
    safety_status: str


class RecheckOut(Schema):
    """Targeted availability recheck: tasks are only queued; the site's crawler visits them."""

    queued: list[int] = Field(description="Page ids now queued for a check.")
    in_progress: list[int] = Field(description="Page ids the crawler is fetching right now.")
    skipped: list[int] = Field(description="Page ids not allowed (disabled site, off-site URL).")


class ClusterRefOut(Schema):
    id: int
    member_count: int


class CountsOut(Schema):
    references: int
    clusters: int
    safety: int
    safety_release: int = Field(
        description="Blocked pages crediting a customer creator, waiting on staff."
    )
    creators: int = Field(description="needs_review creators waiting on staff.")
    claims: int = Field(description="Refused upload claims still open.")
    common_frames: int = Field(description="Proposed common-frame groups waiting on a label.")


# --- References (M2 ground truth) ---


class ReferenceOut(Schema):
    id: int
    title: str
    creator_id: int
    creator_name: str = Field(description="Primary creator (rights holder).")
    creator_names: list[str] = Field(
        description="Everyone performing in the original, the primary creator first."
    )
    duration_s: float | None


class ReferenceSummaryOut(ReferenceOut):
    proposed: int
    proposed_suggestions: int = Field(description="Proposed pairs without a fingerprint match.")
    confirmed: int
    rejected: int
    conflict: int


class LeakOut(Schema):
    id: int
    status: str
    source: str
    score: float
    note: str


Band = Literal["high", "review", "low"]
DecidedBy = Literal["auto", "human"]
CandidateSource = Literal["pdq", "embedding", "metadata"]
#: `matching.copy_extent.CopyExtent` (issue #62): derived by matching, never a filter.
CopyExtentCode = Literal["full_copy", "excerpt", "compilation_candidate", "unknown"]
#: `matching.copy_extent.ExtentReason`: which rule decided the extent.
ExtentReasonCode = Literal[
    "not_classified",
    "photo_collection",
    "no_reference_duration",
    "other_originals",
    "no_timeline",
    "weak_timeline",
    "page_longer",
    "page_shorter",
    "full_span",
    "narrow_span",
    "inconclusive",
]


class ConfidenceSignalOut(Schema):
    """One named confidence component (ADR 0015), derived from `confidence_signals`."""

    key: str = Field(description="Component name as stored, e.g. 'evidence', 'timeline'.")
    label: str
    value: str = Field(description="Readable input of the component, e.g. '6 evidence frames'.")
    weight: float | None = Field(
        description="Maximum contribution (negative for penalties); null for unknown components."
    )
    contribution: float = Field(description="Stored contribution to the confidence sum.")


class MatchOut(Schema):
    id: int
    status: str
    strength: str
    best_distance: int
    evidence_frames: int
    matched_frames: int
    page_frames: int
    best_moment: str
    matched_moments: list[str]
    shared_moments: list[str]
    band: Band
    candidate_source: CandidateSource = Field(
        description="What put this row here (ADR 0037, ADR 0040). 'pdq': a normal match with "
        "frame evidence. 'embedding': the second signal ranked this original near a frame of the "
        "page. 'metadata': the third signal found the page's stored duration, cast and title "
        "consistent with this original and **compared no pictures at all**. For both candidate "
        "sources **nothing has confirmed it** — no evidence frame, no timeline, always the review "
        "band, never the high band. Confirming one is a human judgement about the pictures, not "
        "an agreement with a score."
    )
    confidence: float = Field(description="0..1, clamped sum of the confidence signals.")
    timeline_score: float | None = Field(description="Null: no frame order (posters only).")
    consistent_frames: int = Field(description="Frames in the longest in-order chain.")
    ordered_frames: int | None = Field(
        description="Ordered evidence frames judged, parsed from timeline_reason; null if unknown."
    )
    timeline_reason: str
    loose_frames: int = Field(description="Evidence frames admitted beyond the strict distance.")
    common_frames: int = Field(description="Matching page frames discounted as common frames.")
    common_seconds: list[str] = Field(description="Reference moments (mm:ss) of common frames.")
    decided_by: DecidedBy | None = Field(
        description="Author of the match row's decision as stored; null when none is recorded."
    )
    confidence_signals: list[ConfidenceSignalOut]
    copy_extent: CopyExtentCode = Field(
        description="Full copy, excerpt, compilation candidate or unknown (issue #62); derived, "
        "never a filter. 'unknown' with reason 'not_classified' for a row another classifier "
        "version wrote."
    )
    copy_extent_reason: ExtentReasonCode
    copy_span_s: float | None = Field(
        description="Original seconds from the first to the last in-order matched moment."
    )
    copy_coverage: float | None = Field(description="copy_span_s / original duration, 0..1.")
    copy_other_originals: int = Field(
        description="Other video originals strongly matched on the same page."
    )
    exception_review_required: bool = Field(
        description="A notice would need a licence/exception review first: anything but a "
        "current full copy (`matching.copy_extent.requires_exception_review`)."
    )


class SignalsOut(Schema):
    score: float
    duration_diff_s: float | None
    duration_ratio: float | None
    title_similarity: float
    creator_linked: bool
    matched_strength: str | None
    related_kind: str | None = Field(
        description="Propagated suggestion: 'cluster' or 'mirror' evidence from a strong match."
    )
    related_page_id: int | None = Field(description="The strongly matched page it came from.")


PairStatus = Literal["proposed", "confirmed", "rejected", "conflict"]
StatusFilter = Literal["proposed", "confirmed", "rejected", "conflict", "all"]


class ReferenceItemOut(Schema):
    key: str = Field(description="Stable pair key: r<reference id>-p<page id>.")
    status: PairStatus
    reference: ReferenceOut
    page: PageOut
    signals: SignalsOut
    leak: LeakOut | None
    match: MatchOut | None
    cluster: ClusterRefOut | None
    media: list[MediaRefOut]
    media_hidden: bool = Field(description="True when the page's safety status forbids media.")


class BandThresholdsOut(Schema):
    high: float
    review: float
    calibrated: bool = Field(
        description="False: defaults (no ConfidenceSettings row or too few labels)."
    )


class ReferenceQueueMetaOut(Schema):
    band_thresholds: BandThresholdsOut
    hidden_low: int = Field(
        description="Pairs in scope left out because their match is in the low band."
    )


class ReferenceItemPageOut(Schema):
    items: list[ReferenceItemOut]
    next_cursor: str | None
    meta: ReferenceQueueMetaOut


class PairDecisionOut(Schema):
    decision: Decision
    leak_ids: list[int] = Field(description="ReferenceLeak rows whose status changed.")
    match_ids: list[int] = Field(description="ReferenceMatch rows whose status changed.")


class RejectRemainingIn(Schema):
    include_matches: bool = False
    site_id: int | None = None


# --- Clusters ---

ClusterFilter = Literal["linked", "all"]
Provenance = Literal["system", "pair_label", "reviewer"]


class OriginalLinkOut(Schema):
    """One (reference, page) pair of a member: its ReferenceMatch and/or ReferenceLeak."""

    reference_id: int
    reference_title: str
    creator_name: str = Field(description="Primary creator (rights holder).")
    creator_names: list[str] = Field(
        description="Everyone performing in the original, the primary creator first."
    )
    status: PairStatus = Field(description="Pair status, as in the reference queue.")
    match_strength: str | None
    match_status: str | None
    copy_extent: CopyExtentCode | None = Field(
        description="The match's copy extent (issue #62); null without a match row."
    )
    leak_status: str | None
    leak_source: str | None


class LinkedPageOut(Schema):
    page_id: int
    status: PairStatus
    match_strength: str | None
    copy_extent: CopyExtentCode | None = Field(
        description="The match's copy extent (issue #62); null without a match row."
    )
    leak_status: str | None
    leak_source: str | None


class LinkedOriginalOut(Schema):
    """An original linked to the cluster through active members (rejected pairs excluded)."""

    reference: ReferenceOut
    pages: list[LinkedPageOut] = Field(description="Best evidence first.")


class ClusterMemberOut(Schema):
    id: int
    page: PageOut
    status: str
    evidence: str
    provenance: Provenance = Field(
        description=(
            "Derived, not recorded: 'system' (proposed from evidence), 'pair_label' (confirmed, "
            "evidence=manual and a SAME PairLabel with another member), 'reviewer' (any other "
            "confirmed/rejected status: changed by a person)."
        )
    )
    min_distance: int | None
    borderline: bool
    possible_clip: bool
    originals: list[OriginalLinkOut]
    media: list[MediaRefOut]


class ClusterOut(Schema):
    id: int
    member_count: int
    proposed_count: int
    borderline_count: int
    linked_originals: list[LinkedOriginalOut]
    members: list[ClusterMemberOut]


class ClusterPageOut(Schema):
    items: list[ClusterOut]
    next_cursor: str | None


class MemberDecisionOut(Schema):
    decision: Decision
    member_ids: list[int]
    notes: list[str] = Field(
        default_factory=list, description="Caveats, e.g. a reset that build_clusters will redo."
    )


class MergeIn(Schema):
    target_id: int
    source_ids: list[int] = Field(min_length=1, max_length=50)


class MoveIn(Schema):
    cluster_id: int


# --- Safety (text only: no media fields, ever) ---


class SafetyMatchOut(Schema):
    term: str
    category: str
    action: str


class SafetyItemOut(Schema):
    id: int
    site: str
    title: str
    url: str
    tags: list[str]
    matches: list[SafetyMatchOut]
    categories: list[str]
    flagged_at: datetime | None


class SafetyPageOut(Schema):
    items: list[SafetyItemOut]
    next_cursor: int | None


class BlockIn(IdsIn):
    reason: str = Field(min_length=1, max_length=2000)


class SafetyDecisionOut(Schema):
    status: str
    page_ids: list[int]


# --- Customer release queue ("Flagged term — customer credited", docs/safety.md). Text
# only: no media ids, no image or page URLs, ever — these pages are blocked. ---


class CustomerCreditOut(Schema):
    creator_id: int
    creator_name: str
    matched_in: list[str] = Field(
        description="Where the name appears as whole words: performers, tags, title."
    )


class SafetyReleaseItemOut(Schema):
    id: int
    site: str
    title: str
    tags: list[str]
    performers: list[str]
    matches: list[SafetyMatchOut]
    customer_credits: list[CustomerCreditOut] = Field(
        description="Customer creators (rights holders of owned originals) the page credits now."
    )
    flagged_at: datetime | None
    queued_at: datetime | None


class SafetyReleasePageOut(Schema):
    items: list[SafetyReleaseItemOut]
    next_cursor: int | None


class ReleaseIn(IdsIn):
    reason: str = Field(min_length=1, max_length=2000)


class KeepBlockedIn(IdsIn):
    reason: str = Field(default="", max_length=2000)


# --- Safety incidents (issue #66). No media fields; URLs only in the report draft, which is the
# report itself (docs/safety.md). ---


class IncidentTermOut(Schema):
    category: str
    term: str


class IncidentItemOut(Schema):
    id: int
    reference_id: str
    status: str
    categories: list[str]
    terms: list[IncidentTermOut]
    detected_at: datetime
    last_seen_at: datetime
    reported_at: datetime | None
    reported_via: str
    reference: str
    report_category: str
    closed_at: datetime | None
    url_count: int
    sites: list[str]


class IncidentListOut(Schema):
    items: list[IncidentItemOut]
    truncated: bool


class ReportUrlOut(Schema):
    url: str
    site: str
    evidence: str
    primary: bool
    first_seen_at: datetime
    last_seen_at: datetime
    new_since_report: bool


class ReportDraftTextOut(Schema):
    language: Literal["de", "en"]
    subject: str
    body: str


class ReportDraftOut(Schema):
    incident_id: int
    reference_id: str
    status: str
    template_version: str
    channel: str
    supplement: bool
    reported_at: datetime | None
    reference: str
    media_fetch: Literal["none", "fetched", "unknown"]
    urls: list[ReportUrlOut]
    drafts: list[ReportDraftTextOut]


class IncidentReportedIn(Schema):
    """Record that a human filed the report. A blank `reference` means none was issued."""

    reference: str = Field("", max_length=128)
    via: str = Field("", max_length=255)
    report_category: str = Field("", max_length=255)
    reported_at: datetime | None = None
    language: Literal["de", "en"] | None = None


# --- Creator review queue (catalog.Creator.needs_review, issue #29 follow-up, operator
# 2026-09-17): names a customer typed that matched no existing creator by exact name. ---


class SimilarCreatorOut(Schema):
    """A possibly-matching existing creator: suggestion only, never applied automatically."""

    id: int
    name: str
    aliases: list[str]
    similarity: float = Field(description="0..1, normalised-name similarity.")


class CreatorReviewItemOut(Schema):
    id: int
    name: str
    created_at: datetime
    organisations: list[str] = Field(
        description="Organisation(s) whose customer upload created or reused this name."
    )
    linked_references: int = Field(description="Originals (primary or co-performer) linked so far.")
    similar: list[SimilarCreatorOut] = Field(description="Existing creators with a similar name.")


class CreatorReviewListOut(Schema):
    items: list[CreatorReviewItemOut]
    truncated: bool


class CreatorMarkReviewedOut(Schema):
    id: int
    needs_review: bool


class CreatorMergeIn(Schema):
    target_id: int = Field(description="The existing creator to merge or alias into.")


class CreatorMergeOut(Schema):
    source_id: int
    source_name: str
    target_id: int
    target_name: str
    as_alias: bool
    moved_references: int
    moved_pages: int
    aliases_added: list[str]


# --- Refused upload claims (409 claim_needs_review, issue #29 amendment, operator 2026-09-17):
# the customer sees one neutral 409; staff see why. ---

ClaimStatusFilter = Literal["open", "resolved", "dismissed", "all"]


class ClaimReferenceOut(Schema):
    id: int
    title: str
    creator_name: str


class ClaimItemOut(Schema):
    id: int
    organisation_id: int
    organisation_name: str
    sha256: str
    reason: str = Field(description="owned_by_other / unowned_existing / hash_overlap.")
    overlap_share: float | None
    references: list[ClaimReferenceOut]
    requested_by: str | None
    status: str = Field(description="open / resolved / dismissed.")
    note: str
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None


class ClaimListOut(Schema):
    items: list[ClaimItemOut]
    truncated: bool


class ClaimDecisionIn(Schema):
    note: str = Field("", max_length=2000)


class ClaimConflictOut(Schema):
    detail: str


# --- Common-frame groups (issue #16): candidates from `build_common_frames` (ADR 0015) waiting
# for a human studio label. Sample pages reuse `page_out`/`media_refs`, so a safety-flagged
# member's media is left out by the same server-side gate every other queue uses (never a
# client-side check). ---

CommonFrameKind = Literal["studio_intro", "watermark", "generic", "not_common", "unlabeled"]
CommonFrameStatus = Literal["proposed", "confirmed", "rejected"]
CommonFrameShow = Literal["proposed", "all"]


class CommonFrameMemberFrameOut(Schema):
    """One page frame that belongs to the group — the answer to "which frame of this page is the
    one that repeats" (operator, 2026-09-18: "I couldn't review the group because I don't know
    which frames are the same"). `matching.models.CommonFrameMember` per (group, page)."""

    media_id: int = Field(description="The index.Media row this frame belongs to.")
    frame_index: int = Field(
        description="index.FrameHash.frame_index (0 for a poster/hover-frame/photo: one picture, "
        "one hash)."
    )
    position: int | None = Field(
        description="Order within its own sequence — a hover thumbnail's number parsed from its "
        "URL, or a clip/sprite's sample position (matching.timeline.frame_position); null for an "
        "unordered image such as a poster."
    )
    timestamp_s: float | None = Field(
        description="Seconds into the media this frame was sampled from, only where the sampling "
        "rate is fixed and known (a preview clip, matching.media_hash.PREVIEW_FPS); null "
        "otherwise — FrameHash stores no timestamp of its own, so nothing here is a guess."
    )
    distance: int = Field(
        description="Hamming distance to the group's representative hash "
        "(CommonFrameMember.distance)."
    )
    showable: bool = Field(
        description="Whether GET /media/{id}/image can show exactly this frame alone: true for "
        "a poster, hover frame or photo (one media row is one picture); false for a sprite sheet, "
        "preview clip or browser capture, where the media row holds several frames and the "
        "passthrough would serve the whole thing, not this one."
    )
    kind: str = Field(description="index.Media.Kind of the row above.")


class CommonFrameMemberPageOut(Schema):
    page: PageOut
    media: list[MediaRefOut] = Field(
        description="Empty for a page media_refs would not serve (safety review/blocked/gated)."
    )
    media_gated: bool = Field(
        description="The media gate (ADR 0018) withholds this page's images: `media` is empty "
        "because of the gate, not because the page has none. Without it a reviewer cannot tell "
        "'no image stored' from 'not shown to you' (operator review 2026-09-18)."
    )
    member_frames: list[CommonFrameMemberFrameOut] = Field(
        description="This page's own frame(s) that belong to the group — showable ones first."
    )
    frame_count: int = Field(
        description="How many frame hashes this page holds in total (index.FrameHash), for "
        "judging what the group's frame(s) are relative to the whole page."
    )


class CommonFrameGroupOut(Schema):
    id: int
    kind: CommonFrameKind
    status: CommonFrameStatus
    studio_name: str
    label: str
    member_frame_count: int
    page_count: int
    site_count: int
    labeled_at: datetime | None = Field(
        description="Set by a human label; a labeled group survives the next rebuild as an anchor."
    )
    created_at: datetime
    discounting: bool = Field(
        description="status=confirmed and kind != not_common: this group's frames stop counting "
        "as match evidence (matching.common_frames.DISCOUNTING_STATUSES)."
    )
    sample_pages: list[CommonFrameMemberPageOut] = Field(
        description="Up to 10 pages showing this group's frames (CommonFrameGroup.sample_page_ids)."
    )


class CommonFrameQueueOut(Schema):
    items: list[CommonFrameGroupOut]
    truncated: bool


class CommonFrameLabelIn(Schema):
    kind: CommonFrameKind
    studio_name: str = Field("", max_length=255)
    status: CommonFrameStatus
    label: str = Field("", max_length=255)
