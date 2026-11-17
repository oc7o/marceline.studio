import json
from decimal import Decimal

from django.contrib import admin, messages
from django.db import transaction
from django.db.models import QuerySet, Sum
from django.http import HttpRequest
from django.utils.html import format_html

from catalog.models import Site
from core.pagination import EstimatedCountPaginator
from crawler.discovery.kinds import blocks_onboarding
from crawler.discovery.services import approval_note, undo_approval, undo_approval_blocker
from crawler.models import (
    AdapterHealth,
    AdapterParseStat,
    CrawlTask,
    LLMCall,
    SearchCall,
    SourceCandidate,
    TelegramChannelCandidate,
)


@admin.register(CrawlTask)
class CrawlTaskAdmin(admin.ModelAdmin[CrawlTask]):
    list_display = (
        "url",
        "site",
        "kind",
        "status",
        "priority",
        "attempts",
        "fetched_at",
        "next_attempt_at",
    )
    list_filter = ("status", "kind", "site")
    list_select_related = ("site",)
    search_fields = ("url", "decision_reason", "last_error")
    raw_id_fields = ("discovered_from",)
    # `crawler_crawltask` is big enough (100k+ rows) that an unfiltered count is a real scan
    # (33 ms / 7,292 buffers measured); read Postgres's own estimate instead (core/pagination.py).
    paginator = EstimatedCountPaginator
    # Django's default also runs a second, *unconditional* exact `COUNT(*)` on every load (the
    # changelist's "(N total)" figure, computed whether or not it ends up filtered enough to
    # show) — independent of `paginator` above. Off: a filtered/searched list shows "Show all"
    # instead of an exact total across the whole table; the always-shown result count is still
    # correct (exact when filtered, estimated when not) via `paginator`.
    show_full_result_count = False


@admin.register(LLMCall)
class LLMCallAdmin(admin.ModelAdmin[LLMCall]):
    list_display = (
        "created_at",
        "model",
        "purpose",
        "site",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "task",
    )
    list_filter = ("model", "purpose", "site")
    list_select_related = ("task", "site")
    search_fields = ("input_sha256", "decision", "error")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: LLMCall | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: LLMCall | None = None) -> bool:
        return False


@admin.register(SourceCandidate)
class SourceCandidateAdmin(admin.ModelAdmin[SourceCandidate]):
    list_display = (
        "domain",
        "status",
        "kind",
        "coverage",
        "distinct_creators",
        "search_hits",
        "outlink_pages",
        "cms_guess",
        "blocked_reason",
    )
    list_filter = (
        "status",
        "kind",
        "coverage",
        "cms_guess",
        ("blocked_reason", admin.EmptyFieldListFilter),
    )
    search_fields = ("domain", "notes")
    readonly_fields = (
        "domain",
        "status",
        "first_seen_at",
        "last_seen_at",
        "search_hits",
        "distinct_creators",
        "best_rank",
        "outlink_pages",
        "evidence_text",
        "probed_at",
        "robots_allows",
        "http_status",
        "blocked_reason",
        "title",
        "cms_guess",
        "has_search",
        "video_links_seen",
        "profile_links_seen",
        "forum_links_seen",
        "gallery_images_seen",
        "coverage",
    )
    exclude = ("evidence",)
    actions = ("approve", "reject", "mark_hoster", "mark_torrent", "ignore", "reset")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.display(description="Evidence")
    def evidence_text(self, obj: SourceCandidate) -> str:
        return format_html("<pre>{}</pre>", json.dumps(obj.evidence, indent=2, ensure_ascii=False))

    @admin.action(description="Approve (create disabled Site)")
    def approve(self, request: HttpRequest, queryset: QuerySet[SourceCandidate]) -> None:
        refused: list[str] = []
        with transaction.atomic():
            for candidate in queryset.select_for_update():
                # Our own rules already say no (issue #118): a login wall, a CAPTCHA, a paywall,
                # an age verification gate, a robots.txt disallow, or a kind parked for legal
                # review. The database constraint refuses it too; this says why.
                if blocks_onboarding(candidate.coverage):
                    refused.append(f"{candidate.domain} ({candidate.coverage})")
                    continue
                Site.objects.get_or_create(
                    domain=candidate.domain,
                    defaults={"enabled": False, "notes": approval_note(candidate)},
                )
                candidate.status = SourceCandidate.Status.APPROVED
                candidate.save(update_fields=("status",))
        if refused:
            self.message_user(
                request,
                "Not approved, our own rules forbid covering these "
                "(docs/research/creator-leak-sources.md): " + ", ".join(refused),
                level=messages.WARNING,
            )

    def _change(
        self, request: HttpRequest, queryset: QuerySet[SourceCandidate], **fields: str
    ) -> None:
        """Set fields; leaving `approved` deletes the untouched disabled Site the approval created.

        Approved candidates whose Site is enabled or has crawl data stay approved."""
        kept: list[str] = []
        with transaction.atomic():
            for candidate in queryset.select_for_update():
                if candidate.status == SourceCandidate.Status.APPROVED:
                    if blocker := undo_approval_blocker(candidate):
                        kept.append(f"{candidate.domain} ({blocker})")
                        continue
                    undo_approval(candidate)
                for name, value in fields.items():
                    setattr(candidate, name, value)
                candidate.save(update_fields=tuple(fields))
        if kept:
            self.message_user(
                request,
                "Still approved; disable the Site or remove its data first: " + ", ".join(kept),
                messages.WARNING,
                fail_silently=True,
            )

    @admin.action(description="Reject")
    def reject(self, request: HttpRequest, queryset: QuerySet[SourceCandidate]) -> None:
        self._change(request, queryset, status=SourceCandidate.Status.REJECTED)

    @admin.action(description="Mark hoster")
    def mark_hoster(self, request: HttpRequest, queryset: QuerySet[SourceCandidate]) -> None:
        self._change(
            request,
            queryset,
            status=SourceCandidate.Status.HOSTER,
            kind=SourceCandidate.Kind.FILE_HOSTER,
        )

    @admin.action(description="Mark torrent/P2P source")
    def mark_torrent(self, request: HttpRequest, queryset: QuerySet[SourceCandidate]) -> None:
        self._change(
            request,
            queryset,
            status=SourceCandidate.Status.TORRENT,
            kind=SourceCandidate.Kind.TORRENT,
        )

    @admin.action(description="Ignore")
    def ignore(self, request: HttpRequest, queryset: QuerySet[SourceCandidate]) -> None:
        self._change(request, queryset, status=SourceCandidate.Status.IGNORED)

    @admin.action(description="Reset to proposed (undo approval)")
    def reset(self, request: HttpRequest, queryset: QuerySet[SourceCandidate]) -> None:
        self._change(request, queryset, status=SourceCandidate.Status.PROPOSED)


@admin.register(TelegramChannelCandidate)
class TelegramChannelCandidateAdmin(admin.ModelAdmin[TelegramChannelCandidate]):
    """Text-only review list. Approval is a decision record; it enables no crawling."""

    list_display = (
        "channel",
        "private_invite",
        "status",
        "linking_pages",
        "distinct_creators",
        "distinct_sites",
        "first_seen_at",
        "last_seen_at",
    )
    list_filter = ("status", "private_invite")
    search_fields = ("identity", "notes")
    ordering = ("-linking_pages", "-distinct_creators")
    fields = (
        "channel",
        "private_invite",
        "status",
        "notes",
        "linking_pages",
        "distinct_creators",
        "distinct_sites",
        "sites_text",
        "sample_pages_text",
        "sample_post_ids_text",
        "first_seen_at",
        "last_seen_at",
    )
    readonly_fields = tuple(field for field in fields if field != "notes")
    actions = ("approve", "reject", "ignore")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.display(description="Channel", ordering="identity")
    def channel(self, obj: TelegramChannelCandidate) -> str:
        if obj.private_invite:
            return f"private invite (sha256 {obj.identity[:12]}…): off-limits, not followed"
        url = f"https://t.me/{obj.identity}"
        return format_html('<a href="{}" rel="noopener noreferrer nofollow">{}</a>', url, url)

    @admin.display(description="Sites")
    def sites_text(self, obj: TelegramChannelCandidate) -> str:
        return format_html("<pre>{}</pre>", "\n".join(map(str, obj.sites)))

    @admin.display(description="Sample linking pages")
    def sample_pages_text(self, obj: TelegramChannelCandidate) -> str:
        return format_html("<pre>{}</pre>", "\n".join(map(str, obj.sample_pages)))

    @admin.display(description="Sample post ids")
    def sample_post_ids_text(self, obj: TelegramChannelCandidate) -> str:
        return format_html("<pre>{}</pre>", ", ".join(map(str, obj.sample_post_ids)))

    @admin.action(description="Approve (decision only; enables no crawling)")
    def approve(self, request: HttpRequest, queryset: QuerySet[TelegramChannelCandidate]) -> None:
        skipped = queryset.filter(private_invite=True).count()
        queryset.filter(private_invite=False).update(
            status=TelegramChannelCandidate.Status.APPROVED
        )
        if skipped:
            self.message_user(
                request,
                f"{skipped} private invite link(s) not approved: off-limits.",
                messages.WARNING,
                fail_silently=True,
            )

    @admin.action(description="Reject")
    def reject(self, request: HttpRequest, queryset: QuerySet[TelegramChannelCandidate]) -> None:
        queryset.update(status=TelegramChannelCandidate.Status.REJECTED)

    @admin.action(description="Ignore")
    def ignore(self, request: HttpRequest, queryset: QuerySet[TelegramChannelCandidate]) -> None:
        queryset.update(status=TelegramChannelCandidate.Status.IGNORED)


@admin.register(SearchCall)
class SearchCallAdmin(admin.ModelAdmin[SearchCall]):
    """Read-only audit log and spend ledger (ADR 0013).

    Add, change and delete stay refused: ADR 0013's spend guard and the lifetime total are counted
    from these rows, so deleting one would move the accounting. What expires is the **query text**,
    which `manage.py research_retention` blanks after its period (ADR 0048, issue #103) — the
    deletion of the personal part, without touching the ledger. `query_scrubbed_at` is when that
    happened.
    """

    list_display = (
        "created_at",
        "engine",
        "query",
        "query_scrubbed_at",
        "mode",
        "status",
        "result_count",
        "cost_usd",
    )
    list_filter = ("engine", "mode", "status")
    readonly_fields = ("total_cost",)

    @admin.display(description="Total search spend (all calls, USD)")
    def total_cost(self, obj: SearchCall | None = None) -> str:
        return str(SearchCall.objects.aggregate(total=Sum("cost_usd"))["total"] or Decimal(0))

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: SearchCall | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: SearchCall | None = None) -> bool:
        return False


@admin.register(AdapterHealth)
class AdapterHealthAdmin(admin.ModelAdmin[AdapterHealth]):
    """Is each site's parser still working (issue #22). Read-only: the verdict is computed.

    Change it by fixing the adapter and letting `manage.py adapter_health` (or the scheduler job)
    re-judge it — never by editing the row, which would only hide the break for one pass.
    """

    list_display = (
        "site",
        "status",
        "parses",
        "blind_share",
        "baseline_blind_share",
        "broken_since",
        "llm_repairs_today",
        "checked_at",
    )
    list_filter = ("status",)
    list_select_related = ("site",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: AdapterHealth | None = None) -> bool:
        return False


@admin.register(AdapterParseStat)
class AdapterParseStatAdmin(admin.ModelAdmin[AdapterParseStat]):
    """The hourly counters the verdict above is computed from. Counters only, never content."""

    list_display = (
        "site",
        "hour",
        "parses",
        "no_page",
        "no_title",
        "no_media",
        "no_duration",
        "no_performers",
        "llm_repaired",
    )
    list_filter = ("site",)
    list_select_related = ("site",)
    date_hierarchy = "hour"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: AdapterParseStat | None = None
    ) -> bool:
        return False
