from datetime import datetime
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Prefetch, QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from matching.common_frames import label_common_group
from matching.copy_extent import CLASSIFIER_VERSION
from matching.creators import creator_label
from matching.duration import duration_tolerance, page_duration
from matching.fingerprint import pipeline_label
from matching.models import (
    BrowserCaptureCandidate,
    ClientEvent,
    Cluster,
    ClusterMember,
    CommonFrameGroup,
    CommonFrameMember,
    ConfidenceSettings,
    PairLabel,
    Reference,
    ReferenceLeak,
    ReferenceMatch,
)
from matching.reference_counts import reference_frame_positions, reference_match_count
from matching.services import merge_clusters, move_member


@admin.register(PairLabel)
class PairLabelAdmin(admin.ModelAdmin[PairLabel]):
    list_display = (
        "id",
        "page_a_title",
        "page_a_site",
        "page_b_title",
        "page_b_site",
        "min_distance",
        "label",
    )
    list_display_links = ("id",)
    list_editable = ("label",)
    list_filter = ("label",)
    list_select_related = ("page_a__site", "page_b__site")
    raw_id_fields = ("page_a", "page_b")
    search_fields = ("page_a__title", "page_b__title")

    @admin.display(description="Page A")
    def page_a_title(self, obj: PairLabel) -> str:
        return str(obj.page_a)

    @admin.display(description="Site A")
    def page_a_site(self, obj: PairLabel) -> str:
        return obj.page_a.site.domain

    @admin.display(description="Page B")
    def page_b_title(self, obj: PairLabel) -> str:
        return str(obj.page_b)

    @admin.display(description="Site B")
    def page_b_site(self, obj: PairLabel) -> str:
        return obj.page_b.site.domain


class HasProposedFilter(admin.SimpleListFilter):
    title = "has proposed members"
    parameter_name = "has_proposed"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin[Cluster]
    ) -> tuple[tuple[str, str], ...]:
        return (("yes", "Yes"), ("no", "No"))

    def queryset(self, request: HttpRequest, queryset: QuerySet[Cluster]) -> QuerySet[Cluster]:
        if self.value() in ("yes", "no"):
            condition = Exists(
                ClusterMember.objects.filter(cluster_id=OuterRef("pk"), status="proposed")
            )
            return queryset.filter(condition if self.value() == "yes" else ~condition)
        return queryset


class HasBorderlineFilter(HasProposedFilter):
    title = "has borderline proposals"
    parameter_name = "has_borderline"

    def queryset(self, request: HttpRequest, queryset: QuerySet[Cluster]) -> QuerySet[Cluster]:
        if self.value() in ("yes", "no"):
            condition = Exists(
                ClusterMember.objects.filter(
                    cluster_id=OuterRef("pk"), status="proposed", borderline=True
                )
            )
            return queryset.filter(condition if self.value() == "yes" else ~condition)
        return queryset


class ClusterMemberForm(forms.ModelForm[ClusterMember]):
    class Meta:
        model = ClusterMember
        fields = ("cluster", "page", "status", "evidence", "min_distance")

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        if self.instance.pk and cleaned and "cluster" in cleaned:
            candidate = ClusterMember.objects.get(pk=self.instance.pk)
            for field in ("status", "evidence", "min_distance"):
                if field in cleaned:
                    setattr(candidate, field, cleaned[field])
            try:
                with transaction.atomic():
                    move_member(candidate, cleaned["cluster"])
                    transaction.set_rollback(True)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return cleaned or {}


@admin.register(ClusterMember)
class ClusterMemberAdmin(admin.ModelAdmin[ClusterMember]):
    form = ClusterMemberForm
    list_display = (
        "cluster",
        "page_title",
        "site",
        "status",
        "evidence",
        "min_distance",
        "borderline",
    )
    raw_id_fields = ("cluster", "page")
    list_filter = ("status", "evidence")
    list_select_related = ("cluster", "page__site")
    readonly_fields = ("borderline",)

    def get_readonly_fields(
        self, request: HttpRequest, obj: ClusterMember | None = None
    ) -> tuple[str, ...]:
        return ("borderline", "page") if obj else ("borderline",)

    @admin.display(description="Page title")
    def page_title(self, obj: ClusterMember) -> str:
        return obj.page.title

    @admin.display(description="Site")
    def site(self, obj: ClusterMember) -> str:
        return obj.page.site.domain

    def save_model(
        self,
        request: HttpRequest,
        obj: ClusterMember,
        form: forms.ModelForm[ClusterMember],
        change: bool,
    ) -> None:
        if change:
            saved = move_member(obj, obj.cluster)
            obj.pk = saved.pk
        else:
            super().save_model(request, obj, form, change)


class ClusterMemberInline(admin.TabularInline[ClusterMember, Cluster]):
    model = ClusterMember
    extra = 0
    raw_id_fields = ("page",)
    fields = (
        "page",
        "page_title",
        "site",
        "page_url",
        "uploaded_at",
        "availability",
        "duration",
        "possible_clip",
        "evidence",
        "min_distance",
        "borderline",
        "status",
    )
    readonly_fields = (
        "borderline",
        "page_title",
        "site",
        "page_url",
        "uploaded_at",
        "availability",
        "duration",
        "possible_clip",
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[ClusterMember]:
        return (
            super()
            .get_queryset(request)
            .select_related("page__site")
            .prefetch_related(
                Prefetch(
                    "cluster__members",
                    queryset=ClusterMember.objects.exclude(status="rejected").select_related(
                        "page"
                    ),
                )
            )
        )

    @admin.display(description="Duration")
    def duration(self, obj: ClusterMember) -> str:
        seconds = page_duration(obj.page.raw_extraction)
        if seconds is None:
            return "—"
        minutes, remainder = divmod(int(seconds), 60)
        return f"{minutes:02d}:{remainder:02d}"

    @admin.display(description="Possible clip", boolean=True)
    def possible_clip(self, obj: ClusterMember) -> bool:
        seconds = page_duration(obj.page.raw_extraction)
        longest = max(
            (
                page_duration(member.page.raw_extraction) or 0
                for member in obj.cluster.members.all()
            ),
            default=0,
        )
        return seconds is not None and longest - seconds > duration_tolerance(longest)

    @admin.display(description="Title")
    def page_title(self, obj: ClusterMember) -> str:
        return obj.page.title

    @admin.display(description="Site")
    def site(self, obj: ClusterMember) -> str:
        return obj.page.site.domain

    @admin.display(description="URL")
    def page_url(self, obj: ClusterMember) -> str:
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>',
            obj.page.url,
            obj.page.url,
        )

    @admin.display(description="Uploaded at")
    def uploaded_at(self, obj: ClusterMember) -> datetime | None:
        return obj.page.uploaded_at

    @admin.display(description="Availability")
    def availability(self, obj: ClusterMember) -> str:
        return obj.page.availability


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin[Cluster]):
    list_display = ("id", "member_count", "representative_title", "sites", "has_proposed")
    list_filter = (HasProposedFilter, HasBorderlineFilter)
    actions = ("merge_selected_clusters",)
    list_select_related = ()
    inlines = (ClusterMemberInline,)

    @admin.action(description="Merge selected clusters")
    def merge_selected_clusters(self, request: HttpRequest, queryset: QuerySet[Cluster]) -> None:
        clusters = list(queryset.order_by("pk"))
        if len(clusters) < 2:
            self.message_user(request, "Select at least two clusters.", messages.ERROR)
            return
        try:
            merge_clusters(clusters[0], clusters[1:])
        except ValueError as exc:
            self.message_user(request, str(exc), messages.ERROR)
        else:
            self.message_user(request, f"Merged into cluster {clusters[0].pk}.", messages.SUCCESS)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Cluster]:
        return (
            super()
            .get_queryset(request)
            .annotate(
                proposed_flag=Exists(
                    ClusterMember.objects.filter(cluster_id=OuterRef("pk"), status="proposed")
                )
            )
            .prefetch_related(
                Prefetch(
                    "members",
                    queryset=ClusterMember.objects.exclude(status="rejected")
                    .select_related("page__site")
                    .order_by(
                        F("page__uploaded_at").asc(nulls_last=True), "page__first_seen", "page_id"
                    ),
                )
            )
        )

    @admin.display(description="Members")
    def member_count(self, obj: Cluster) -> int:
        return len(obj.members.all())

    @admin.display(description="Representative")
    def representative_title(self, obj: Cluster) -> str:
        return next((str(m.page) for m in obj.members.all()), "—")

    @admin.display(description="Sites")
    def sites(self, obj: Cluster) -> str:
        return ", ".join(sorted({m.page.site.domain for m in obj.members.all()}))

    @admin.display(description="Has proposed", boolean=True)
    def has_proposed(self, obj: Cluster) -> bool:
        return any(m.status == "proposed" for m in obj.members.all())


MOMENTS_SHOWN = 12


def format_moments(
    matched: list[int],
    shared: list[int],
    common: list[int] | None = None,
    *,
    photos: bool = False,
) -> str:
    """Matched reference positions; long lists truncated.

    Video seconds as mm:ss, e.g. `16:52, 16:53 · shared: 49:40`. A photo collection has no time:
    its positions are image ordinals and are shown as `#3` (ADR 0020).
    """

    def clock(values: list[int]) -> str:
        text = ", ".join(
            (f"#{s}" if photos else f"{s // 60:02d}:{s % 60:02d}") for s in values[:MOMENTS_SHOWN]
        )
        extra = len(values) - MOMENTS_SHOWN
        return f"{text} (+{extra})" if extra > 0 else text

    parts = [clock(matched)] if matched else []
    if shared:
        parts.append(f"shared: {clock(shared)}")
    if common:
        parts.append(f"common: {clock(common)}")
    return " · ".join(parts) or "—"


@admin.register(Reference)
class ReferenceAdmin(admin.ModelAdmin[Reference]):
    list_display = (
        "title",
        "kind",
        "performers",
        "duration_s",
        "frames_count",
        "shared_frames_count",
        "matches_count",
        "pipeline_grid",
        "uploaded_by",
    )
    list_filter = ("kind", "pipeline")
    list_select_related = ("creator", "uploaded_by")
    raw_id_fields = ("creator",)
    # The co-performers of an original (issue #64); the primary creator stays the raw-id FK above.
    filter_horizontal = ("creators",)
    readonly_fields = (
        "kind",
        "sha256",
        "duration_s",
        "frame_fps",
        "pipeline",
        "client",
        "uploaded_by",
        "created_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def get_queryset(self, request: HttpRequest) -> QuerySet[Reference]:
        return (
            super()
            .get_queryset(request)
            .prefetch_related("creators")
            .annotate(
                frame_total=reference_frame_positions(),
                shared_total=reference_frame_positions(shared=True),
                match_total=reference_match_count(),
            )
        )

    @admin.display(description="Creators", ordering="creator__name")
    def performers(self, obj: Reference) -> str:
        """Primary creator first, e.g. "Sasha Meyer + Carla Bremer"."""
        return creator_label(obj)

    @admin.display(description="Frames")
    def frames_count(self, obj: Reference) -> int:
        return int(getattr(obj, "frame_total", 0))

    @admin.display(description="Shared frames")
    def shared_frames_count(self, obj: Reference) -> int:
        return int(getattr(obj, "shared_total", 0))

    @admin.display(description="Matches")
    def matches_count(self, obj: Reference) -> int:
        return int(getattr(obj, "match_total", 0))

    @admin.display(description="Pipeline", ordering="pipeline")
    def pipeline_grid(self, obj: Reference) -> str:
        """The recipe tag and what it means for the sampling grid (issue #70, ADR 0022).

        Sorting and the filter still use the raw `pipeline` field, so "show me everything still on
        the superseded grid" is one click in the sidebar.
        """
        return pipeline_label(obj.pipeline)


@admin.register(ReferenceMatch)
class ReferenceMatchAdmin(admin.ModelAdmin[ReferenceMatch]):
    list_display = (
        "reference",
        "page_title",
        "site",
        "page_url",
        # First among the columns that look like a verdict: a reviewer must be able to see at a
        # glance that a row holds no evidence at all (ADR 0037, ADR 0040).
        "candidate_source",
        "band",
        "confidence",
        "strength",
        "evidence_frames",
        "timeline_score",
        "copy_extent_display",
        "moments",
        "best_distance",
        "frame_ratio",
        "cluster_link",
        "status",
        "decided_by",
    )
    list_editable = ("status",)
    list_filter = (
        "status",
        "candidate_source",
        "band",
        "strength",
        "copy_extent",
        "decided_by",
        "reference__creator",
        "page__site",
    )
    actions = ("revert_auto_decisions",)
    list_select_related = ("reference", "page__site", "cluster")
    raw_id_fields = ("reference", "page", "cluster")
    readonly_fields = (
        "reference",
        "page",
        "cluster",
        # Never editable: it is what the three walls of ADR 0037 and ADR 0040 are keyed on.
        "candidate_source",
        "strength",
        "band",
        "confidence",
        "confidence_signals",
        "decided_by",
        "evidence_frames",
        "loose_frames",
        "common_frames",
        "timeline_score",
        "consistent_frames",
        "timeline_reason",
        "copy_extent",
        "copy_extent_reason",
        "copy_span_s",
        "copy_coverage",
        "copy_other_originals",
        "copy_extent_version",
        "matched_seconds",
        "shared_seconds",
        "common_seconds",
        "best_distance",
        "matched_frames",
        "page_frames",
        "best_reference_timestamp_s",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def save_model(
        self,
        request: HttpRequest,
        obj: ReferenceMatch,
        form: forms.ModelForm[ReferenceMatch],
        change: bool,
    ) -> None:
        if change and "status" in form.changed_data:
            obj.decided_by = ReferenceMatch.DecidedBy.HUMAN
            # The date a notice draft snapshots as its human review (issue #142); a row put back to
            # `proposed` has no decision to date.
            obj.decided_at = (
                None if obj.status == ReferenceMatch.Status.PROPOSED else timezone.now()
            )
        super().save_model(request, obj, form, change)

    @admin.action(description="Revert auto decisions to proposed")
    def revert_auto_decisions(
        self, request: HttpRequest, queryset: QuerySet[ReferenceMatch]
    ) -> None:
        count = queryset.filter(decided_by=ReferenceMatch.DecidedBy.AUTO).update(
            status=ReferenceMatch.Status.PROPOSED,
            decided_by=ReferenceMatch.DecidedBy.HUMAN,
            decided_at=None,
        )
        self.message_user(request, f"Reverted {count} auto decisions.", messages.SUCCESS)

    @admin.display(description="Page title")
    def page_title(self, obj: ReferenceMatch) -> str:
        return obj.page.title

    @admin.display(description="Site")
    def site(self, obj: ReferenceMatch) -> str:
        return obj.page.site.domain

    @admin.display(description="Page URL")
    def page_url(self, obj: ReferenceMatch) -> str:
        return format_html(
            '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>',
            obj.page.url,
            obj.page.url,
        )

    @admin.display(description="Copy extent", ordering="copy_extent")
    def copy_extent_display(self, obj: ReferenceMatch) -> str:
        """Extent, why, and the span behind it; "not classified" for another classifier version."""
        if obj.copy_extent_version != CLASSIFIER_VERSION:
            return "not classified"
        text = f"{obj.copy_extent.replace('_', ' ')} ({obj.copy_extent_reason.replace('_', ' ')})"
        if obj.copy_coverage is not None:
            text += f", {obj.copy_coverage:.0%} of the original"
        return text

    @admin.display(description="Matched / considered frames")
    def frame_ratio(self, obj: ReferenceMatch) -> str:
        return f"{obj.matched_frames}/{obj.page_frames}"

    @admin.display(description="Moments")
    def moments(self, obj: ReferenceMatch) -> str:
        return format_moments(
            obj.matched_seconds,
            obj.shared_seconds,
            obj.common_seconds,
            photos=obj.reference.kind == Reference.Kind.PHOTOS,
        )

    @admin.display(description="Cluster")
    def cluster_link(self, obj: ReferenceMatch) -> str:
        if obj.cluster_id is None:
            return "—"
        return format_html(
            '<a href="{}">Cluster {}</a>',
            reverse("admin:matching_cluster_change", args=(obj.cluster_id,)),
            obj.cluster_id,
        )


LABEL_FIELDS = frozenset({"kind", "studio_name", "status", "label"})


@admin.register(CommonFrameGroup)
class CommonFrameGroupAdmin(admin.ModelAdmin[CommonFrameGroup]):
    list_display = (
        "id",
        "label",
        "kind",
        "studio_name",
        "status",
        "page_count",
        "site_count",
        "member_frame_count",
        "sample_page_ids",
        "labeled_at",
    )
    list_filter = ("kind", "status")
    search_fields = ("label", "studio_name")
    fields = (
        "label",
        "kind",
        "studio_name",
        "status",
        "representative_hex",
        "max_distance",
        "member_frame_count",
        "page_count",
        "site_count",
        "sample_page_ids",
        "members_link",
        "labeled_at",
        "created_at",
        "updated_at",
    )
    readonly_fields = (
        "representative_hex",
        "max_distance",
        "member_frame_count",
        "page_count",
        "site_count",
        "sample_page_ids",
        "members_link",
        "labeled_at",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def save_model(
        self,
        request: HttpRequest,
        obj: CommonFrameGroup,
        form: forms.ModelForm[CommonFrameGroup],
        change: bool,
    ) -> None:
        if change and LABEL_FIELDS & set(form.changed_data):
            label_common_group(obj, obj.kind, obj.studio_name, obj.status, obj.label)
        else:
            super().save_model(request, obj, form, change)

    @admin.display(description="Representative PDQ")
    def representative_hex(self, obj: CommonFrameGroup) -> str:
        return bytes(obj.representative_hash).hex()

    @admin.display(description="Member frames")
    def members_link(self, obj: CommonFrameGroup) -> str:
        return format_html(
            '<a href="{}?group__id__exact={}">Pages showing these frames</a>',
            reverse("admin:matching_commonframemember_changelist"),
            obj.pk,
        )


@admin.register(CommonFrameMember)
class CommonFrameMemberAdmin(admin.ModelAdmin[CommonFrameMember]):
    list_display = ("group", "page", "frame", "distance")
    list_select_related = ("group", "page")
    raw_id_fields = ("group", "page", "frame")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: CommonFrameMember | None = None
    ) -> bool:
        return False


@admin.register(ConfidenceSettings)
class ConfidenceSettingsAdmin(admin.ModelAdmin[ConfidenceSettings]):
    list_display = (
        "high_threshold",
        "review_threshold",
        "calibrated",
        "labeled_positives",
        "labeled_negatives",
        "updated_at",
    )
    readonly_fields = (
        "high_threshold",
        "review_threshold",
        "calibrated",
        "labeled_positives",
        "labeled_negatives",
        # What calibration derived, beside what is in force: they differ exactly when a human set
        # the thresholds, which is what `calibration.operator_override` protects (issue #90).
        "calibrated_high",
        "calibrated_review",
        "report",
        "updated_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


class LeakMatchedFilter(admin.SimpleListFilter):
    title = "matched"
    parameter_name = "matched"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin[ReferenceLeak]
    ) -> tuple[tuple[str, str], ...]:
        return (("yes", "Yes"), ("no", "No"))

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[ReferenceLeak]
    ) -> QuerySet[ReferenceLeak]:
        condition = Exists(
            ReferenceMatch.objects.filter(
                reference_id=OuterRef("reference_id"), page_id=OuterRef("page_id")
            )
        )
        if self.value() in ("yes", "no"):
            return queryset.filter(condition if self.value() == "yes" else ~condition)
        return queryset


@admin.register(ReferenceLeak)
class ReferenceLeakAdmin(admin.ModelAdmin[ReferenceLeak]):
    list_display = (
        "reference_label",
        "page_title",
        "site",
        "page_url",
        "duration",
        "title_similarity",
        "matched_strength",
        "score",
        "status",
    )
    list_display_links = ("reference_label",)
    list_editable = ("status",)
    list_filter = ("status", "reference", "page__site", LeakMatchedFilter)
    ordering = ("reference", "-score")
    list_select_related = ("reference__creator", "page__site")
    raw_id_fields = ("reference", "page")
    readonly_fields = ("created_at", "updated_at")
    actions = ("confirm", "reject")

    def get_queryset(self, request: HttpRequest) -> QuerySet[ReferenceLeak]:
        return (
            super()
            .get_queryset(request)
            .prefetch_related("page__reference_matches", "reference__creators")
        )

    @admin.display(description="Reference")
    def reference_label(self, obj: ReferenceLeak) -> str:
        return f"{creator_label(obj.reference)}: {obj.reference.title}"

    @admin.display(description="Page title")
    def page_title(self, obj: ReferenceLeak) -> str:
        return obj.page.title

    @admin.display(description="Site")
    def site(self, obj: ReferenceLeak) -> str:
        return obj.page.site.domain

    @admin.display(description="Page URL")
    def page_url(self, obj: ReferenceLeak) -> str:
        if not obj.page.url.startswith(("https://", "http://")):
            return obj.page.url
        return format_html(
            '<a href="{}" rel="noopener noreferrer">{}</a>', obj.page.url, obj.page.url
        )

    @admin.display(description="Page / reference duration (diff)")
    def duration(self, obj: ReferenceLeak) -> str:
        def clock(value: float | None) -> str:
            return "—" if value is None else f"{int(value) // 60:02d}:{int(value) % 60:02d}"

        page = page_duration(obj.page.raw_extraction)
        original = obj.reference.duration_s
        diff = f"{page - original:+.0f}s" if page is not None and original is not None else "—"
        return f"{clock(page)} / {clock(original)} ({diff})"

    @admin.display(description="Title similarity")
    def title_similarity(self, obj: ReferenceLeak) -> str:
        return str(obj.signals.get("title_similarity", "—"))

    @admin.display(description="Matched strength")
    def matched_strength(self, obj: ReferenceLeak) -> str:
        return next(
            (
                row.strength
                for row in obj.page.reference_matches.all()
                if row.reference_id == obj.reference_id
            ),
            "—",
        )

    @admin.action(description="Confirm")
    def confirm(self, request: HttpRequest, queryset: QuerySet[ReferenceLeak]) -> None:
        queryset.update(status="confirmed", updated_at=timezone.now())

    @admin.action(description="Reject")
    def reject(self, request: HttpRequest, queryset: QuerySet[ReferenceLeak]) -> None:
        queryset.update(status="rejected", updated_at=timezone.now())


@admin.register(ClientEvent)
class ClientEventAdmin(admin.ModelAdmin[ClientEvent]):
    """Why browser fingerprinting runs fail (issue #29 counters, input for cloud import #59).

    Read-only: these rows are the counter. Filter by kind to read `codec_unsupported` and
    `client_failed` off the changelist.
    """

    list_display = ("created_at", "kind", "container", "codec", "user", "detail")
    list_filter = ("kind", "container")
    list_select_related = ("user",)
    ordering = ("-created_at",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: ClientEvent | None = None) -> bool:
        return False


@admin.register(BrowserCaptureCandidate)
class BrowserCaptureCandidateAdmin(admin.ModelAdmin[BrowserCaptureCandidate]):
    """Browser-capture triage queue (ADR 0016): view only, text only, highest priority first.

    Rows are written by `triage_browser_capture`; deleting a row lets triage queue the page
    again (an operator decision)."""

    list_display = (
        "page_id",
        "page_title",
        "site",
        "reason",
        "priority",
        "status",
        "reference",
        "attempts",
        "queued_at",
        "decided_at",
        "last_error",
    )
    list_display_links = ("page_id",)
    list_filter = ("status", "reason", "site")
    ordering = ("-priority", "pk")
    list_select_related = ("page", "site", "reference")
    search_fields = ("page__title", "page__url")
    fields = (
        "page_id",
        "page_title",
        "page_url_text",
        "site",
        "reference",
        "reason",
        "priority",
        "status",
        "signals",
        "queued_at",
        "decided_at",
        "attempts",
        "last_error",
        "capture_run",
        "refusal_detail",
        "updated_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: BrowserCaptureCandidate | None = None
    ) -> bool:
        return False

    @admin.display(description="Page title")
    def page_title(self, obj: BrowserCaptureCandidate) -> str:
        return obj.page.title

    @admin.display(description="Page URL (text)")
    def page_url_text(self, obj: BrowserCaptureCandidate) -> str:
        return obj.page.url
