"""Ground-truth labels for tuning the PDQ match threshold."""

from django.conf import settings
from django.db import models
from django.db.models.functions import Length, Mod
from django.db.models.lookups import Exact, GreaterThanOrEqual
from django.utils import timezone

from index.models import Page
from matching.confidence import DEFAULT_HIGH_THRESHOLD, DEFAULT_REVIEW_THRESHOLD
from matching.copy_extent import COPY_EXTENT_CHOICES, EXTENT_REASON_CHOICES
from matching.crop_grid import STAGE1_CROPS
from matching.embedding import SKETCH_BYTES
from matching.pdq import PDQ_HASH_BYTES


class PairLabel(models.Model):
    """A human judgement whether two pages show the same video.

    Pairs are stored ordered (page_a.id < page_b.id) so each pair exists at most once;
    use matching.services.label_pair to create them.
    """

    class Label(models.TextChoices):
        SAME = "same"
        DIFFERENT = "different"
        UNSURE = "unsure"

    page_a = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="+")
    page_b = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="+")
    label = models.CharField(max_length=16, choices=Label, default=Label.UNSURE)
    min_distance = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Smallest PDQ Hamming distance when labeled."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("page_a", "page_b"), name="pairlabel_unique_pair"),
            models.CheckConstraint(
                condition=models.Q(page_a__lt=models.F("page_b")), name="pairlabel_ordered_pages"
            ),
        )

    def __str__(self) -> str:
        return f"{self.page_a_id} ~ {self.page_b_id}: {self.label}"


class Cluster(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"Cluster {self.pk}"


class ClusterMember(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "proposed"
        CONFIRMED = "confirmed"
        REJECTED = "rejected"

    class Evidence(models.TextChoices):
        PDQ = "pdq"
        SHA256 = "sha256"
        EXTERNAL_LINK = "external_link"
        MIRROR = "mirror"
        MANUAL = "manual"

    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE, related_name="members")
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="cluster_memberships")
    status = models.CharField(max_length=16, choices=Status, default=Status.PROPOSED)
    evidence = models.CharField(max_length=16, choices=Evidence)
    min_distance = models.SmallIntegerField(null=True, blank=True)
    borderline = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("cluster", "page"), name="cluster_unique_page"),
            models.UniqueConstraint(
                fields=("page",),
                condition=~models.Q(status="rejected"),
                name="cluster_one_active_membership",
            ),
        )

    def __str__(self) -> str:
        return f"{self.cluster_id}: {self.page_id} ({self.status})"


class Reference(models.Model):
    """One original: a video, or a whole photo collection (ADR 0010, ADR 0020).

    A photo collection is N unrelated images with no order, kept as ONE reference because a leak
    reposts the set. Its `sha256` is the collection digest (`matching.fingerprint.collection_
    sha256`), and it has neither `duration_s` nor `frame_fps` — a database constraint enforces
    that, so a collection can never pretend to have a timeline.
    """

    class Kind(models.TextChoices):
        VIDEO = "video"
        PHOTOS = "photos"

    kind = models.CharField(
        max_length=8,
        choices=Kind,
        default=Kind.VIDEO,
        help_text="Video original, or a photo collection (many images, no timeline).",
    )
    creator = models.ForeignKey(
        "catalog.Creator",
        on_delete=models.PROTECT,
        related_name="references",
        help_text="Primary creator (rights holder); always also in `creators`.",
    )
    creators = models.ManyToManyField(
        "catalog.Creator",
        related_name="referenced_in",
        blank=True,
        help_text="Everyone performing in this original, the primary creator included. "
        "Kept in sync by matching.creators.set_creators, not by the database.",
    )
    title = models.CharField(max_length=255)
    duration_s = models.FloatField(
        null=True, blank=True, help_text="Video length; always empty for a photo collection."
    )
    sha256 = models.CharField(
        max_length=64,
        help_text="The file's digest for a video; the collection digest for a photo collection. "
        "Unique per pipeline, so one original can be re-fingerprinted under a newer recipe "
        "while the old reference keeps working (issue #70, ADR 0022).",
    )
    source_note = models.TextField(blank=True, help_text="Free text; no personal data.")
    frame_fps = models.FloatField(
        null=True,
        blank=True,
        help_text="Samples per second of a video; always empty for a photo collection.",
    )
    pipeline = models.CharField(max_length=64, default="ffmpeg-rgb512-pdq-v0")
    embedding_pipeline = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_default="",
        help_text="Recipe that produced this original's frame sketches (ADR 0037); empty = none. "
        "A tag of its own beside `pipeline`, as ADR 0022 requires: a page sketch is only ever "
        "compared with a reference sketch carrying the same tag.",
    )
    client = models.JSONField(
        default=dict,
        blank=True,
        help_text="Where the fingerprints were computed; no personal data.",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_references",
        help_text="Account that registered this original through the browser client. "
        "A claim of rights, never proof of them; null for operator/CLI imports.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            # One row per (original, recipe). The digest alone was unique until issue #70, which
            # made re-fingerprinting necessary: the v1 grid samples different moments of the same
            # file, so the operator imports the new hashes *alongside* the old reference, confirms
            # them, and only then retires the old row. Two rows with the same digest and the same
            # pipeline would still be the same hashes twice, and are refused.
            # The sampling rate is part of that identity too (2026-09-17, ADR 0022 amendment):
            # a 2 fps re-fingerprint of a 1 fps original hashes twice the moments and imports
            # beside it. NULLs are not distinct, so a photo collection (no fps) stays unique per
            # (digest, pipeline).
            models.UniqueConstraint(
                fields=("sha256", "pipeline", "frame_fps"),
                name="reference_unique_sha256_pipeline_fps",
                nulls_distinct=False,
            ),
            # A video samples at a positive rate; a photo collection has no rate and no duration.
            # `frame_fps__isnull=False` is not redundant: in SQL `NULL > 0` is NULL, not false,
            # and `NULL OR false` is NULL, which a CHECK constraint accepts. Without it a video
            # could be stored with no sampling rate at all.
            models.CheckConstraint(
                condition=(
                    models.Q(kind="video", frame_fps__isnull=False, frame_fps__gt=0)
                    | models.Q(kind="photos", frame_fps__isnull=True, duration_s__isnull=True)
                ),
                name="reference_shape_matches_kind",
            ),
        )

    def __str__(self) -> str:
        return self.title


class ReferenceFrameHash(models.Model):
    """One hashed position of an original, in one orientation.

    For a video, `frame_index` is the sample position and `timestamp_s` is `frame_index /
    frame_fps` seconds. For a photo collection, `frame_index` is the image's ordinal and
    `timestamp_s` repeats it: a position, never a time (ADR 0020). Anything rendering it as
    mm:ss must check `Reference.kind` first.
    """

    reference = models.ForeignKey(Reference, on_delete=models.CASCADE, related_name="frames")
    frame_index = models.PositiveIntegerField(
        help_text="Video sample position, or the image's ordinal in a photo collection."
    )
    timestamp_s = models.FloatField(
        help_text="Seconds into the video; the image ordinal again for a photo collection."
    )
    pdq = models.BinaryField(max_length=32)
    quality = models.PositiveSmallIntegerField()
    dihedral = models.PositiveSmallIntegerField(default=0)
    embedding = models.BinaryField(
        max_length=SKETCH_BYTES,
        null=True,
        blank=True,
        help_text="512-bit sign sketch of this moment (ADR 0037). Only on the unflipped "
        "orientation: an embedding is roughly flip-invariant, so one sketch per moment, not eight.",
    )
    crop_grid = models.BinaryField(
        null=True,
        blank=True,
        help_text="This moment's crop lattice (ADR 0039): N x 32 bytes in `matching.crop_grid."
        "grid_specs` order, the 16 corner crops first. One blob, never 256 rows — rows would cost "
        "486 bytes each (9.6 GB for the library) where the blob is 8.2 KB per moment and 629 MB. "
        "Unflipped orientation only: mirroring stays the job of the eight dihedral hashes.",
    )
    shared = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Also appears in other references or unrelated pages (e.g. distributor intro).",
    )

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("reference", "frame_index", "dihedral"),
                name="reference_unique_frame_orientation",
            ),
            models.CheckConstraint(
                condition=models.Q(dihedral__lte=7), name="reference_dihedral_range"
            ),
            models.CheckConstraint(
                condition=Exact(Length("pdq"), 32), name="reference_pdq_32_bytes"
            ),
            # One sketch per moment, on the unflipped orientation only, 64 bytes when present.
            models.CheckConstraint(
                condition=models.Q(embedding__isnull=True)
                | (Exact(Length("embedding"), SKETCH_BYTES) & models.Q(dihedral=0)),
                name="reference_embedding_64_bytes_dihedral_zero",
            ),
            # A crop grid is a whole number of PDQ hashes, at least the stage-1 prefix, on the
            # unflipped orientation only. The prefix bound is the one that matters: stage 1 reads
            # the first STAGE1_CROPS entries of the blob and would otherwise search whatever bytes
            # happen to be there (`crop_grid.corner_hashes` refuses the same thing in Python).
            models.CheckConstraint(
                condition=models.Q(crop_grid__isnull=True)
                | (
                    models.Q(dihedral=0)
                    & GreaterThanOrEqual(Length("crop_grid"), STAGE1_CROPS * PDQ_HASH_BYTES)
                    & Exact(Mod(Length("crop_grid"), PDQ_HASH_BYTES), 0)
                ),
                name="reference_crop_grid_whole_hashes_dihedral_zero",
            ),
        )

    def __str__(self) -> str:
        return f"{self.reference_id}#{self.frame_index}/{self.dihedral}"


class ReferenceMatchQuerySet(models.QuerySet["ReferenceMatch"]):
    def review_queue(self, *, include_low: bool = False) -> "ReferenceMatchQuerySet":
        """Proposed matches, high band first, then by confidence; low band hidden by default."""
        rows = self.filter(status="proposed")
        if not include_low:
            rows = rows.exclude(band="low")
        return rows.annotate(
            band_order=models.Case(
                models.When(band="high", then=0),
                models.When(band="review", then=1),
                default=2,
                output_field=models.IntegerField(),
            )
        ).order_by("band_order", "-confidence", "pk")


class ReferenceMatch(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "proposed"
        CONFIRMED = "confirmed"
        REJECTED = "rejected"

    class Strength(models.TextChoices):
        STRONG = "strong"
        WEAK = "weak"

    class Band(models.TextChoices):
        HIGH = "high"
        REVIEW = "review"
        LOW = "low"

    class DecidedBy(models.TextChoices):
        AUTO = "auto"
        HUMAN = "human"

    class CandidateSource(models.TextChoices):
        PDQ = "pdq"
        EMBEDDING = "embedding"
        METADATA = "metadata"

    #: Sources that are candidate generators, never evidence (ADR 0037, ADR 0040). A row carrying
    #: one of them holds no frame evidence, is always in the `review` band, and is excluded
    #: wherever a match counts as proof: the notice gate, the customer's finding list, re-banding.
    CANDIDATE_ONLY_SOURCES = ("embedding", "metadata")

    reference = models.ForeignKey(Reference, on_delete=models.CASCADE, related_name="matches")
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="reference_matches")
    cluster = models.ForeignKey(
        Cluster, null=True, blank=True, on_delete=models.SET_NULL, related_name="reference_matches"
    )
    best_distance = models.PositiveSmallIntegerField()
    matched_frames = models.PositiveIntegerField()
    page_frames = models.PositiveIntegerField()
    best_reference_timestamp_s = models.FloatField()
    evidence_frames = models.PositiveIntegerField(
        default=0, help_text="Distinct page frames matching non-shared reference frames."
    )
    matched_seconds = models.JSONField(
        default=list, blank=True, help_text="Reference seconds matched by non-shared frames."
    )
    shared_seconds = models.JSONField(
        default=list, blank=True, help_text="Reference seconds matched only by shared frames."
    )
    common_seconds = models.JSONField(
        default=list,
        blank=True,
        help_text="Reference seconds matched only by page frames in active common-frame groups.",
    )
    common_frames = models.PositiveIntegerField(
        default=0, help_text="Matching page frames discounted as common (intro, watermark, ...)."
    )
    loose_frames = models.PositiveIntegerField(
        default=0,
        help_text="Evidence frames beyond max distance, admitted by timeline or corroboration.",
    )
    timeline_score = models.FloatField(
        null=True, blank=True, help_text="0..1 order/spacing consistency; empty without order."
    )
    consistent_frames = models.PositiveIntegerField(
        default=0, help_text="Largest order-consistent subset of ordered evidence frames."
    )
    timeline_reason = models.TextField(blank=True)
    confidence = models.FloatField(default=0)
    band = models.CharField(max_length=8, choices=Band, default=Band.LOW, db_index=True)
    confidence_signals = models.JSONField(default=dict, blank=True)
    strength = models.CharField(max_length=8, choices=Strength, default=Strength.WEAK)
    # Copy extent (issue #62, `matching.copy_extent`): derived by every matching run, never a
    # filter — an excerpt is a match like any other. The notice gate asks
    # `copy_extent.requires_exception_review`, which distrusts rows of another classifier version.
    # `db_default`s so a worker still running the previous code can insert rows after migrating.
    copy_extent = models.CharField(
        max_length=24,
        choices=COPY_EXTENT_CHOICES,
        default="unknown",
        db_default="unknown",
        help_text="Full copy, excerpt, compilation candidate or unknown; derived, not a decision.",
    )
    copy_extent_reason = models.CharField(
        max_length=32,
        choices=EXTENT_REASON_CHOICES,
        default="not_classified",
        db_default="not_classified",
        help_text="Which rule decided the copy extent.",
    )
    copy_span_s = models.FloatField(
        null=True,
        blank=True,
        help_text="Original seconds from the first to the last in-order matched moment, plus one "
        "sample period; empty without a verified timeline.",
    )
    copy_coverage = models.FloatField(
        null=True,
        blank=True,
        help_text="copy_span_s / original duration, 0..1; empty without span or duration.",
    )
    copy_other_originals = models.PositiveSmallIntegerField(
        default=0,
        db_default=0,
        help_text="Other video originals (other files) strongly matched on the same page.",
    )
    copy_extent_version = models.PositiveSmallIntegerField(
        default=0,
        db_default=0,
        help_text="Classifier version that wrote the extent; 0 = never classified.",
    )
    status = models.CharField(max_length=16, choices=Status, default=Status.PROPOSED)
    # What put this row here (ADR 0037, ADR 0040). `pdq` is a normal match with frame evidence.
    # `embedding` is a *candidate*: the second signal ranked this original near one of the page's
    # frames and nothing has confirmed it. An embedding has no safe fixed threshold (measured: its
    # nearest unrelated pair scores above a typical true pair). `metadata` is the third signal's
    # candidate: the page's stored duration, cast and title are consistent with this original and
    # no picture was compared at all — the fields are the pirate site's own text, which a re-title
    # defeats and a spam farm can forge. Neither carries an evidence frame, neither can be in the
    # high band, and neither can reach the notice gate, which needs `high` plus a human
    # confirmation. A later PDQ match takes the same row over and sets this to `pdq` — that is the
    # promotion path, together with a human confirming it in review.
    candidate_source = models.CharField(
        max_length=16,
        choices=CandidateSource,
        default=CandidateSource.PDQ,
        db_default="pdq",
        db_index=True,
        help_text="pdq = frame evidence; embedding or metadata = a candidate awaiting proof.",
    )
    decided_by = models.CharField(
        max_length=8,
        choices=DecidedBy,
        blank=True,
        default="",
        help_text="Who set the current confirmed/rejected status; auto decisions are reversible.",
    )
    # When the current decision was taken (issue #142). `updated_at` cannot answer that: it is
    # `auto_now`, so an extent classification or a candidate-source promotion long afterwards moves
    # it — and this date is snapshotted into a customer's notice draft and printed in the notice's
    # evidence paragraph, where naming a review later than it happened would overstate
    # what was reviewed. Cleared when a decision is reset.
    decided_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the current confirmed/rejected decision was taken. Empty while proposed.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ReferenceMatchQuerySet.as_manager()

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("reference", "page"), name="reference_unique_page"),
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0, confidence__lte=1),
                name="reference_match_confidence_range",
            ),
            models.CheckConstraint(
                condition=models.Q(copy_coverage__isnull=True)
                | models.Q(copy_coverage__gte=0, copy_coverage__lte=1),
                name="reference_match_copy_coverage_range",
            ),
            # The invariant of ADR 0037, in the database rather than only in the code that writes
            # these rows: an embedding candidate holds no evidence and is never in the high band.
            # `matching.confidence.band_for` and `matching.calibration.apply_bands` enforce the
            # same thing independently; this is the wall that catches a future writer.
            models.CheckConstraint(
                condition=~models.Q(candidate_source="embedding")
                | models.Q(band="review", evidence_frames=0, matched_frames=0, loose_frames=0),
                name="reference_match_embedding_is_never_evidence",
            ),
            # The same invariant for the third signal (ADR 0040), as its own constraint rather
            # than a widened version of the one above: each signal's wall should fail with its own
            # name, and ADR 0037's guarantee must not become collateral of an edit to this one.
            # Metadata compares no pictures at all, so a row it proposed holding evidence frames
            # would be a straight contradiction.
            models.CheckConstraint(
                condition=~models.Q(candidate_source="metadata")
                | models.Q(band="review", evidence_frames=0, matched_frames=0, loose_frames=0),
                name="reference_match_metadata_is_never_evidence",
            ),
        )

    def __str__(self) -> str:
        return f"{self.reference_id}: {self.page_id} ({self.status})"


class ConfidenceSettings(models.Model):
    """Single row (pk=1): band thresholds written by `calibrate_confidence`, read by matching."""

    high_threshold = models.FloatField(default=DEFAULT_HIGH_THRESHOLD)
    review_threshold = models.FloatField(default=DEFAULT_REVIEW_THRESHOLD)
    calibrated = models.BooleanField(
        default=False, help_text="False: conservative defaults (too few labels)."
    )
    labeled_positives = models.PositiveIntegerField(default=0)
    labeled_negatives = models.PositiveIntegerField(
        default=0,
        help_text="Rejected pairs with a stored match; a rejection nothing scored is "
        "not a negative sample (issue #90).",
    )
    # What calibration itself last derived, beside what is in force. They differ exactly when a
    # human set the thresholds by hand, which is how `calibration.operator_override` recognises an
    # operator override without asking anyone to tick a box. NULL = written before this pair
    # existed, i.e. provenance unknown, which is read as an override.
    calibrated_high = models.FloatField(
        null=True, blank=True, help_text="High threshold calibration last derived; empty = unknown."
    )
    calibrated_review = models.FloatField(
        null=True,
        blank=True,
        help_text="Review threshold calibration last derived; empty = unknown.",
    )
    report = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "confidence settings"
        constraints = (
            models.CheckConstraint(condition=models.Q(id=1), name="confidence_settings_singleton"),
            models.CheckConstraint(
                condition=models.Q(
                    review_threshold__gte=0,
                    review_threshold__lte=models.F("high_threshold"),
                    high_threshold__lte=1,
                ),
                name="confidence_settings_threshold_order",
            ),
        )

    def __str__(self) -> str:
        return f"high>={self.high_threshold:.2f}, review>={self.review_threshold:.2f}"


class CommonFrameGroup(models.Model):
    """Near-identical page frames seen on many unrelated pages: intros, logos, black cards."""

    class Kind(models.TextChoices):
        STUDIO_INTRO = "studio_intro"
        WATERMARK = "watermark"
        GENERIC = "generic"
        NOT_COMMON = "not_common"
        UNLABELED = "unlabeled"

    class Status(models.TextChoices):
        PROPOSED = "proposed"
        CONFIRMED = "confirmed"
        REJECTED = "rejected"

    representative_hash = models.BinaryField(max_length=32)
    max_distance = models.PositiveSmallIntegerField(help_text="Member radius around the hash.")
    member_frame_count = models.PositiveIntegerField(default=0)
    page_count = models.PositiveIntegerField(default=0)
    site_count = models.PositiveIntegerField(default=0)
    sample_page_ids = models.JSONField(default=list, blank=True)
    label = models.CharField(max_length=255, blank=True)
    kind = models.CharField(max_length=16, choices=Kind, default=Kind.UNLABELED)
    studio_name = models.CharField(max_length=255, blank=True, db_index=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.PROPOSED)
    labeled_at = models.DateTimeField(
        null=True, blank=True, help_text="Set by a human label; labeled groups survive rebuilds."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=Exact(Length("representative_hash"), 32),
                name="common_group_hash_32_bytes",
            ),
        )

    def __str__(self) -> str:
        name = self.label or self.studio_name or f"Common frames {self.pk}"
        return f"{name} ({self.kind}, {self.page_count} pages)"


class CommonFrameMember(models.Model):
    """A page frame belonging to a common-frame group; lists pages showing e.g. an intro."""

    group = models.ForeignKey(CommonFrameGroup, on_delete=models.CASCADE, related_name="members")
    frame = models.ForeignKey(
        "index.FrameHash", on_delete=models.CASCADE, related_name="common_memberships"
    )
    page = models.ForeignKey(
        Page, on_delete=models.CASCADE, related_name="common_frame_memberships"
    )
    distance = models.PositiveSmallIntegerField()

    class Meta:
        constraints = (models.UniqueConstraint(fields=("frame",), name="common_frame_one_group"),)
        indexes = (models.Index(fields=("group", "page"), name="common_member_group_page_idx"),)

    def __str__(self) -> str:
        return f"{self.group_id}: frame {self.frame_id} (page {self.page_id})"


class MediaReuse(models.Model):
    """A media row finished without hashing: its frame hashes are copies of an identical media's.

    `url_key`: a known mirror CDN serves the same image under another URL (decided before any
    download). `sha256`: the downloaded bytes equal a hashed media's bytes (no PDQ/ffmpeg run).
    """

    class Reason(models.TextChoices):
        URL_KEY = "url_key"
        SHA256 = "sha256"

    media = models.OneToOneField("index.Media", on_delete=models.CASCADE, related_name="reuse")
    source = models.ForeignKey(
        "index.Media", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reason = models.CharField(max_length=16, choices=Reason)
    key = models.CharField(max_length=2000, blank=True, help_text="URL key or sha256 that matched.")
    frames = models.PositiveIntegerField(default=0, help_text="Frame hashes copied.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.media_id} <- {self.source_id} ({self.reason})"


class ReferenceCandidate(models.Model):
    """Scan cache of incremental matching: a page frame within the matching radius of at least
    one quality-eligible frame of a reference (smallest distance). Aggregates are recomputed from
    these frames on every run; only the Hamming scan is incremental (ADR 0015)."""

    frame = models.ForeignKey("index.FrameHash", on_delete=models.CASCADE, related_name="+")
    reference = models.ForeignKey(Reference, on_delete=models.CASCADE, related_name="candidates")
    distance = models.PositiveSmallIntegerField()

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("frame", "reference"), name="reference_candidate_unique_frame"
            ),
        )

    def __str__(self) -> str:
        return f"{self.reference_id}: frame {self.frame_id} ({self.distance})"


class EmbeddingCandidate(models.Model):
    """A page frame whose sketch ranks one reference moment among its nearest neighbours (0037).

    Deliberately a table of its own rather than a flavour of `ReferenceCandidate`: that one is the
    PDQ scan cache, every consumer of it reads `distance` as a Hamming distance between PDQ
    hashes, and an embedding distance is not that number and must never be mistaken for it. Rows
    here feed `matching.embedding_candidates.refresh_matches` and nothing else — no evidence
    aggregation, no timeline, no shared-frame logic, no confidence component.

    `distance` is the sketch Hamming distance in bits (0..512) and exists to rank and to explain,
    never to threshold. `mutual` records that the page frame is also among that reference moment's
    own nearest neighbours, which is the pilot's criterion.
    """

    frame = models.ForeignKey("index.FrameHash", on_delete=models.CASCADE, related_name="+")
    reference = models.ForeignKey(
        Reference, on_delete=models.CASCADE, related_name="embedding_candidates"
    )
    reference_frame = models.ForeignKey(
        ReferenceFrameHash, on_delete=models.CASCADE, related_name="+"
    )
    distance = models.PositiveSmallIntegerField(help_text="Sketch Hamming distance, 0..512 bits.")
    rank = models.PositiveSmallIntegerField(help_text="Position in the page frame's top-k, 1 up.")
    mutual = models.BooleanField(
        default=False, help_text="The page frame is in that moment's nearest neighbours too."
    )
    pipeline = models.CharField(
        max_length=64, help_text="Embedding recipe both sides were computed with."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("frame", "reference"), name="embedding_candidate_unique_frame"
            ),
            models.CheckConstraint(
                condition=models.Q(distance__lte=512), name="embedding_candidate_distance_range"
            ),
        )

    def __str__(self) -> str:
        return f"{self.reference_id}: frame {self.frame_id} (sketch {self.distance})"


class MatchRunState(models.Model):
    """Single row (pk=1): what the `match_references` candidate scan covers."""

    fingerprint = models.CharField(
        max_length=64, blank=True, help_text="Digest of the settings of the last run."
    )
    settings = models.JSONField(default=dict, blank=True, help_text="Inputs of the fingerprint.")
    reference_signatures = models.JSONField(
        default=dict, blank=True, help_text="Reference id -> [eligible frames, last frame id]."
    )
    frames_scanned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Page frames created before this (minus overlap) are scanned.",
    )
    last_mode = models.CharField(max_length=16, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_full_at = models.DateTimeField(null=True, blank=True)
    last_scanned_frames = models.PositiveIntegerField(default=0)
    last_candidates = models.PositiveIntegerField(default=0)
    last_scan_seconds = models.FloatField(default=0)

    class Meta:
        constraints = (
            models.CheckConstraint(condition=models.Q(id=1), name="match_run_state_singleton"),
        )

    def __str__(self) -> str:
        return f"match run state ({self.last_mode or 'never run'})"


class BrowserCaptureCandidate(models.Model):
    """A page triaged for a future headless-browser frame capture (ADR 0016).

    Only the queue: nothing captures anything yet. `matching.capture_triage` refreshes queued rows
    (priority, reason, signals) and never touches captured rows. At most one open (queued or
    failed) row per page; a captured page is re-queued only when its URL or media changed
    substantially (`matching.triage.changed_substantially`).
    """

    class Status(models.TextChoices):
        QUEUED = "queued"
        SKIPPED = "skipped"
        CAPTURED = "captured"
        FAILED = "failed"
        EXPIRED = "expired"

    class Reason(models.TextChoices):
        REVIEW_BAND_MATCH = "review_band_match"
        CONFIRMED_LEAK_WITHOUT_FRAMES = "confirmed_leak_without_frames"
        LINKED_CLUSTER_MEMBER = "linked_cluster_member"
        EXACT_DURATION_SINGLE_THUMB = "exact_duration_single_thumb"
        # ADR 0040 amendment 2026-09-18: the page's stored text is consistent with an original.
        # The only source that rests on the site's own words rather than on our evidence, which
        # is why it aims a capture and never a review row.
        METADATA_CANDIDATE = "metadata_candidate"

    OPEN_STATUSES = (Status.QUEUED, Status.FAILED)

    page = models.ForeignKey(
        Page, on_delete=models.CASCADE, related_name="browser_capture_candidates"
    )
    reference = models.ForeignKey(
        Reference,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="browser_capture_candidates",
        help_text="Reference of the leading source, if any.",
    )
    site = models.ForeignKey(
        "catalog.Site", on_delete=models.CASCADE, related_name="browser_capture_candidates"
    )
    reason = models.CharField(max_length=32, choices=Reason, help_text="Leading source.")
    priority = models.FloatField(default=0, db_index=True)
    signals = models.JSONField(
        default=dict, blank=True, help_text="Sources, components, frame counts and URL/media basis."
    )
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED, db_index=True)
    queued_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(
        null=True, blank=True, help_text="When the row left the queue (capture, skip, expiry)."
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, help_text="Skip, expiry or failure reason.")
    capture_run = models.JSONField(
        default=dict,
        blank=True,
        help_text="Placeholder for the future capture worker (run id, frames, skip reason). "
        "Empty for rows decided by triage.",
    )
    refusal_detail = models.JSONField(
        default=dict,
        blank=True,
        help_text="Issue #78 amendment: reconstructable detail of a retryable skip that never "
        "opened a browser — a safety/availability deny (page id + rule) or the page's own "
        "robots.txt (host + path shape). Never a URL. Kept separate from capture_run, which "
        "marks a page scanned once (ADR 0016) — a retryable skip must never look like one.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("page",),
                condition=models.Q(status__in=("queued", "failed")),
                name="browser_capture_one_open_per_page",
            ),
        )
        indexes = (models.Index(fields=("status", "-priority"), name="browser_capture_queue_idx"),)

    def __str__(self) -> str:
        return f"page {self.page_id}: {self.reason} {self.priority:.3f} ({self.status})"


class ReferenceLeak(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "proposed"
        CONFIRMED = "confirmed"
        REJECTED = "rejected"

    class Source(models.TextChoices):
        SUGGESTED = "suggested"
        MANUAL = "manual"
        MATCH = "match"
        CLUSTER = "cluster"

    reference = models.ForeignKey(Reference, on_delete=models.CASCADE, related_name="leaks")
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="reference_leaks")
    status = models.CharField(max_length=16, choices=Status, default=Status.PROPOSED)
    source = models.CharField(max_length=16, choices=Source, default=Source.MANUAL)
    score = models.FloatField(default=0)
    signals = models.JSONField(default=dict, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("reference", "page"), name="reference_leak_unique_page"
            ),
        )

    def __str__(self) -> str:
        return f"{self.reference_id}: {self.page_id} ({self.status})"


class LeakCase(models.Model):
    """One original's case: the living state the customer follows (docs/customer-ui-plan.md §3).

    A leak is not a row, it is a case. One case per original, many findings — the same "one case,
    many URLs" shape `index.SafetyIncident` uses for hotline reports (issue #66), because the
    operator's definition of *handled* is "removed **and** not re-uploaded anywhere": a case is
    not closed by a successful takedown, only by one that stayed closed. So a re-upload is a new
    `CaseFinding` on the **same** case, and a case that had gone quiet reopens instead of a second
    case being opened.

    **This table stores observations, not derivations.** Everything derivable from
    `ReferenceMatch`, `ReferenceLeak` and `Page` is derived at read time and is not duplicated
    here; what is stored is exactly what no other table records — *when* a transition was first
    seen. `Page.availability` says a page is gone but not since when, and `availability_checked_at`
    moves with every later check. Guessing those dates would be inventing data, which is the one
    thing the case file must not do.

    Written by `marceline/app/cases.py` (the reconciliation is idempotent), never by a signal and
    never by the crawler. A case whose findings all disappear — rejected by a reviewer, or
    safety-flagged — is deleted with them: an empty case row with `reopen_count = 2` would itself
    tell a customer that something is being hidden from them.
    """

    class State(models.TextChoices):
        # At least one finding is not known to be gone ("unknown" is not "removed").
        OPEN = "open"
        # Every finding is gone, and the case stays under watch — never archived.
        REMOVED_WATCHING = "removed_watching"

    reference = models.OneToOneField(Reference, on_delete=models.CASCADE, related_name="case")
    state = models.CharField(max_length=24, choices=State, default=State.OPEN, db_index=True)
    opened_at = models.DateTimeField(help_text="Earliest finding of this case; the 'found' step.")
    removed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the case last became fully removed; NULL while anything is still up.",
    )
    reopened_at = models.DateTimeField(
        null=True, blank=True, help_text="When a re-upload or a returning page last reopened it."
    )
    reopen_count = models.PositiveIntegerField(
        default=0, help_text="How often this case reopened after having been fully removed."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=~models.Q(state="removed_watching") | models.Q(removed_at__isnull=False),
                name="leak_case_removed_has_date",
            ),
        )

    def __str__(self) -> str:
        return f"case for reference {self.reference_id} ({self.state})"


class CaseFinding(models.Model):
    """One page carrying the original, and when each step of it was first observed.

    A finding is the customer-facing unit of a case: one URL. Its stamps are written once, when a
    transition is *first* seen, so a timeline never moves backwards and never claims a date the
    data cannot support:

    - `discovered_at` — when matching attached this page to the original (the match's or leak's
      own `created_at`), not when this row was written;
    - `verified_at` — when a confirmation was first observed here. The decision rows carry only
      `updated_at`, so that is what a decision made before this table existed contributes;
    - `removed_at` — the first availability check that found the page gone. A later check moves
      `Page.availability_checked_at`, never this;
    - `reappeared_at` — the same URL is live again. Set instead of clearing `removed_at`, so the
      removals log can show an entry that reopened rather than quietly dropping it. Removed again,
      the pair is re-armed: `removed_at` moves and this goes back to NULL.
    """

    case = models.ForeignKey(LeakCase, on_delete=models.CASCADE, related_name="findings")
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="case_findings")
    discovered_at = models.DateTimeField(help_text="When this page was attached to the case.")
    verified_at = models.DateTimeField(
        null=True, blank=True, help_text="First observed confirmation; NULL while under review."
    )
    removed_at = models.DateTimeField(
        null=True, blank=True, help_text="First availability check that found the page gone."
    )
    reappeared_at = models.DateTimeField(
        null=True, blank=True, help_text="This URL is live again after having been removed."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("case", "page"), name="case_finding_unique_page"),
            models.CheckConstraint(
                condition=models.Q(reappeared_at__isnull=True) | models.Q(removed_at__isnull=False),
                name="case_finding_reappeared_after_removal",
            ),
        )

    def __str__(self) -> str:
        return f"case {self.case_id}: page {self.page_id}"


class ClientEvent(models.Model):
    """A browser fingerprinting run that produced no hashes (issue #29; input for #59).

    These rows *are* the `codec_unsupported` / `client_failed` counters: how often the browser
    path fails is what decides whether server-side ("cloud") fingerprinting is worth building
    (`docs/research/cloud-fingerprinting.md`).

    Deliberately minimal: no file name, size or path, only what the decoder reported. A failure
    must never describe someone's unreleased video.
    """

    DETAIL_LENGTH = 200
    SHORT_LENGTH = 64

    class Kind(models.TextChoices):
        CODEC_UNSUPPORTED = "codec_unsupported"
        CLIENT_FAILED = "client_failed"

    kind = models.CharField(max_length=32, choices=Kind, db_index=True)
    detail = models.CharField(
        max_length=DETAIL_LENGTH, blank=True, help_text="Decoder message; never a file name."
    )
    container = models.CharField(max_length=SHORT_LENGTH, blank=True)
    codec = models.CharField(max_length=SHORT_LENGTH, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.kind} ({self.codec or 'unknown codec'})"
