import time
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.db import models
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from catalog.safety_rules import has_release_queue_match
from core.pagination import EstimatedCountPaginator
from index.incidents import mark_closed, mark_false_positive, mark_reported
from index.models import (
    FrameHash,
    Media,
    Page,
    ProspectSampleCheck,
    SafetyIncident,
    SafetyIncidentPage,
    SafetyRuleDecision,
    SiteCreatorBreadcrumb,
)
from index.services import set_safety_status

BLOCKED_NOTICE = (
    "SAFETY BLOCK: do not open, download or screenshot this page or its media. Follow the "
    "incident procedure in docs/safety.md. Media are hidden here. Changing the status requires "
    "a reason in 'Safety note'."
)
INCIDENT_NOTICE = (
    "SAFETY INCIDENT: judge this case from the URL, the matched term and the timestamps only. "
    "Never open, download or screenshot the page or its media (docs/safety.md). Record the "
    "hotline's reference number here once you have one; leave it blank if none was issued."
)
#: Settled cases: the twelve-month retention clock runs from `closed_at` (index/incidents.py).
CLOSED_STATUSES = (SafetyIncident.Status.CLOSED, SafetyIncident.Status.FALSE_POSITIVE)


#: How long a `CachedValuesFilter` keeps the vocabulary it read.
VALUES_CACHE_TTL_S = 300.0
_cached_values: dict[str, tuple[float, tuple[str, ...]]] = {}


def stored_values(
    model: type[models.Model], field: str, now: float | None = None
) -> tuple[str, ...]:
    """The distinct non-empty values of `field`, re-read at most every `VALUES_CACHE_TTL_S`.

    Django's filter for a free-text field runs `SELECT DISTINCT <field> … ORDER BY 1` on every
    changelist load, which on `index_page` (111 MB) and `index_media` (67 MB) is a sequential scan
    for a handful of values. Both vocabularies only change when the code that writes them changes
    — the score reasons live in `crawler/safety_score.py`, a content type comes off the wire — so
    a list a few minutes stale is as good as a fresh one.
    """
    key = f"{model._meta.label}.{field}"
    now = time.monotonic() if now is None else now
    cached = _cached_values.get(key)
    if cached is None or now - cached[0] >= VALUES_CACHE_TTL_S:
        values = tuple(
            sorted(
                value
                for value in model._default_manager.values_list(field, flat=True).distinct()
                if value
            )
        )
        _cached_values[key] = (now, values)
        return values
    return cached[1]


class CachedValuesFilter(admin.SimpleListFilter):
    """A sidebar filter on a free-text field whose value list is cached, not re-scanned.

    Subclasses set `title` and `parameter_name`; the parameter name is the field.
    """

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin[Any]
    ) -> list[tuple[str, str]]:
        values = stored_values(model_admin.model, str(self.parameter_name))
        return [(value, value) for value in values]

    def queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> QuerySet[Any]:
        value = self.value()
        return queryset if value is None else queryset.filter(**{str(self.parameter_name): value})


class ScoreReasonFilter(CachedValuesFilter):
    title = "safety score reason"
    parameter_name = "safety_score_reason"


class ContentTypeFilter(CachedValuesFilter):
    title = "content type"
    parameter_name = "content_type"


class MediaInline(admin.TabularInline[Media, Page]):
    model = Media
    extra = 0
    fields = ("kind", "url", "status", "eligible_at", "sha256", "width", "height", "fetched_at")
    show_change_link = True


class PageAdminForm(forms.ModelForm[Page]):
    """ModelAdmin supplies Meta (model and fields)."""

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        was_blocked = self.instance.pk and self.initial.get("safety_status") == (
            Page.SafetyStatus.BLOCKED
        )
        status = cleaned.get("safety_status")
        if (
            was_blocked
            and status != Page.SafetyStatus.BLOCKED
            and has_release_queue_match(self.instance.safety_matches)
        ):
            # Operator decision 2026-09-17: a customer_release_queue page is released only through
            # the moderation UI's customer release queue, which re-checks the text and the credit.
            self.add_error(
                "safety_status",
                "This page is blocked by a customer_release_queue rule: use Safety review → "
                "customer release queue (docs/safety.md).",
            )
        if was_blocked and status != Page.SafetyStatus.BLOCKED:
            note = str(cleaned.get("safety_note") or "").strip()
            if not note or "safety_note" not in self.changed_data:
                self.add_error("safety_note", "Give the reason for changing a blocked page.")
        return cleaned


@admin.register(Page)
class PageAdmin(admin.ModelAdmin[Page]):
    form = PageAdminForm
    list_display = (
        "title",
        "site",
        "safety_status",
        "safety_score",
        "safety_score_reason",
        "uploaded_at",
        "first_seen",
        "last_seen",
        "availability",
        "availability_checked_at",
        "active",
    )
    list_filter = (
        "safety_status",
        "safety_release",
        ScoreReasonFilter,
        "site",
        "availability",
        "active",
    )
    list_select_related = ("site",)
    # `public_id` is what a customer's case-file URL shows for a copy (ADR 0033).
    search_fields = ("title", "url", "=public_id")
    filter_horizontal = ("creators",)
    raw_id_fields = ("notices",)
    # `index_page` (111 MB) makes an unfiltered count a real scan; read Postgres's own estimate
    # instead (core/pagination.py), and drop the second, unconditional exact count Django's
    # default also runs for the changelist's "(N total)" figure (see crawler.admin.CrawlTaskAdmin
    # for the full reasoning — identical here).
    paginator = EstimatedCountPaginator
    show_full_result_count = False
    readonly_fields = (
        "public_id",
        "safety_matches",
        "safety_flagged_at",
        "safety_reviewed_at",
        "safety_score",
        "safety_score_reason",
        "safety_score_signals",
        "safety_scored_at",
        "safety_rule_version",
        "safety_release",
        "safety_release_creator",
        "safety_release_at",
        # Targeted indexing (issue #71) owns these: `deactivate_untargeted`, never a hand edit.
        "active",
        "inactive_reason",
        "active_changed_at",
        # Provenance (ADR 0041, issue #22): who produced the fields below — the site's adapter,
        # or a model repairing for a broken one. The crawl writes it; editing it by hand would
        # only launder a guess into the site's own words.
        "extraction_source",
    )
    inlines = (MediaInline,)
    actions = ("mark_clear",)

    def get_inlines(self, request: HttpRequest, obj: Page | None) -> list[type[Any]]:
        if obj is not None and obj.safety_status == Page.SafetyStatus.BLOCKED:
            return []
        return list(super().get_inlines(request, obj))

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        if request.method == "GET":
            page = self.get_object(request, object_id)
            if page is not None and page.safety_status == Page.SafetyStatus.BLOCKED:
                messages.error(request, BLOCKED_NOTICE)
        return super().change_view(request, object_id, form_url, extra_context)

    def save_model(self, request: HttpRequest, obj: Page, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        if change and "safety_status" in form.changed_data:
            set_safety_status(
                obj,
                Page.SafetyStatus(obj.safety_status),
                note=obj.safety_note,
                by=request.user.get_username(),
            )

    @admin.action(description="Mark as clear (reviewed) and release media")
    def mark_clear(self, request: HttpRequest, queryset: QuerySet[Page]) -> None:
        pages = list(queryset.filter(safety_status=Page.SafetyStatus.REVIEW))
        for page in pages:
            set_safety_status(page, Page.SafetyStatus.CLEAR, by=request.user.get_username())
        self.message_user(request, f"Marked {len(pages)} review page(s) as clear.")
        skipped = queryset.exclude(safety_status=Page.SafetyStatus.REVIEW).count()
        if skipped:
            self.message_user(
                request,
                f"Left {skipped} page(s) unchanged: only review pages can be cleared in bulk; "
                "blocked pages need an individual change with a reason (docs/safety.md).",
                messages.WARNING,
            )


class FrameHashInline(admin.TabularInline[FrameHash, Media]):
    model = FrameHash
    extra = 0
    can_delete = False
    fields = ("frame_index", "quality", "pdq_hex")
    readonly_fields = ("frame_index", "quality", "pdq_hex")

    @admin.display(description="PDQ (hex)")
    def pdq_hex(self, obj: FrameHash) -> str:
        return bytes(obj.pdq).hex()

    def has_add_permission(self, request: HttpRequest, obj: Media | None = None) -> bool:
        return False


class SafetyIncidentPageInline(admin.TabularInline[SafetyIncidentPage, SafetyIncident]):
    """The URLs of one case. The URL is the whole point of the record — nothing else of the page
    is here, and nothing in this screen ever offers to open it."""

    model = SafetyIncidentPage
    extra = 0
    fields = ("url", "site_domain", "evidence", "page", "first_seen_at", "last_seen_at")
    readonly_fields = ("first_seen_at",)
    raw_id_fields = ("page",)


@admin.register(SafetyIncident)
class SafetyIncidentAdmin(admin.ModelAdmin[SafetyIncident]):
    """The safety incident workflow (issue #66): report a case, record what the hotline said,
    close it or mark it a false positive. Retention is applied by `safety_retention`."""

    list_display = (
        "id",
        "status",
        "category",
        "term",
        "detected_at",
        "last_seen_at",
        "reported_at",
        "reference",
        "closed_at",
        "decided_by",
    )
    list_filter = ("status", "category", "reported_via")
    search_fields = ("term", "reference", "incident_pages__url")
    date_hierarchy = "detected_at"
    readonly_fields = ("terms", "created_at", "updated_at")
    inlines = (SafetyIncidentPageInline,)
    actions = ("mark_reported_action", "mark_closed_action", "mark_false_positive_action")

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        if request.method == "GET":
            messages.warning(request, INCIDENT_NOTICE)
        return super().change_view(request, object_id, form_url, extra_context)

    def save_model(
        self, request: HttpRequest, obj: SafetyIncident, form: Any, change: bool
    ) -> None:
        """Stamp the decision the form implies, so a status change is never left undated."""
        if not obj.decided_by:
            obj.decided_by = request.user.get_username()
        if obj.status == SafetyIncident.Status.REPORTED and obj.reported_at is None:
            obj.reported_at = timezone.now()
        if obj.status in CLOSED_STATUSES and obj.closed_at is None:
            obj.closed_at = timezone.now()
        super().save_model(request, obj, form, change)

    @admin.action(description="Mark as reported (add the reference number in the form)")
    def mark_reported_action(
        self, request: HttpRequest, queryset: QuerySet[SafetyIncident]
    ) -> None:
        incidents = list(queryset.filter(status=SafetyIncident.Status.OPEN))
        for incident in incidents:
            mark_reported(incident, by=request.user.get_username())
        self.message_user(
            request,
            f"Marked {len(incidents)} incident(s) reported. Open each one to record the channel "
            "and the hotline's reference number; leave the reference blank if none was issued.",
        )

    @admin.action(description="Close (the case is finished)")
    def mark_closed_action(self, request: HttpRequest, queryset: QuerySet[SafetyIncident]) -> None:
        incidents = list(queryset.exclude(status__in=CLOSED_STATUSES))
        for incident in incidents:
            mark_closed(incident, by=request.user.get_username())
        self.message_user(
            request,
            f"Closed {len(incidents)} incident(s). The record is deleted twelve months from now "
            "by `safety_retention` (docs/safety.md).",
        )

    @admin.action(description="Mark as a false positive (judged from the text only)")
    def mark_false_positive_action(
        self, request: HttpRequest, queryset: QuerySet[SafetyIncident]
    ) -> None:
        incidents = list(queryset.exclude(status__in=CLOSED_STATUSES))
        for incident in incidents:
            mark_false_positive(incident, by=request.user.get_username())
        self.message_user(
            request,
            f"Marked {len(incidents)} incident(s) as false positives. The page's own safety "
            "status is unchanged: unblock it in Index → Pages with a reason if it was wrong.",
        )


@admin.register(Media)
class MediaAdmin(admin.ModelAdmin[Media]):
    list_display = (
        "url",
        "kind",
        "status",
        "eligible_at",
        "attempts",
        "page",
        "byte_size",
        "fetched_at",
    )
    list_filter = ("status", "kind", "page__safety_status", "page__site", ContentTypeFilter)
    list_select_related = ("page",)
    search_fields = ("url", "sha256", "last_error")
    raw_id_fields = ("page",)
    inlines = (FrameHashInline,)
    # `index_media` (67 MB): same reasoning as `PageAdmin` above.
    paginator = EstimatedCountPaginator
    show_full_result_count = False


@admin.register(SiteCreatorBreadcrumb)
class SiteCreatorBreadcrumbAdmin(admin.ModelAdmin[SiteCreatorBreadcrumb]):
    """Which creators a site roughly carries (issue #71). Read-only: the crawler and
    `deactivate_untargeted --apply` write it. Counts and dates only, never a URL."""

    list_display = ("creator", "site", "page_count", "first_seen", "last_seen")
    list_filter = ("site",)
    list_select_related = ("creator", "site")
    search_fields = ("creator__name", "site__domain")
    ordering = ("creator__name", "-page_count")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: SiteCreatorBreadcrumb | None = None
    ) -> bool:
        return False


@admin.register(ProspectSampleCheck)
class ProspectSampleCheckAdmin(admin.ModelAdmin[ProspectSampleCheck]):
    """Verdicts on a prospect's verification sample (`prospect_report`, docs/prospect-research.md).
    Open the sampled page ids in the moderation UI, then record here whether each page really
    carries the prospect's content. Names and clusters only, never faces."""

    list_display = ("creator", "page", "verdict", "checked_by", "checked_at")
    list_filter = ("verdict",)
    list_select_related = ("creator",)
    raw_id_fields = ("creator", "page")
    search_fields = ("creator__name", "=page__id")
    readonly_fields = ("checked_at",)

    def save_model(
        self, request: HttpRequest, obj: ProspectSampleCheck, form: Any, change: bool
    ) -> None:
        if not obj.checked_by:
            obj.checked_by = request.user.get_username()
        super().save_model(request, obj, form, change)


@admin.register(SafetyRuleDecision)
class SafetyRuleDecisionAdmin(admin.ModelAdmin[SafetyRuleDecision]):
    """Audit log of rule-driven and staff safety decisions (docs/safety.md). Read-only."""

    list_display = ("created_at", "kind", "page", "status", "rule_version", "decided_by")
    list_filter = ("kind", "status")
    list_select_related = ("page",)
    raw_id_fields = ("page", "creator")
    search_fields = ("=page__id", "rule_version", "decided_by")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
