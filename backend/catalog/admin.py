from datetime import datetime, timedelta
from typing import Any

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import BigIntegerField, OuterRef, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.forms import CharField, ModelForm, Textarea, TextInput
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from catalog import policy_audit, robots_policy, site_objections
from catalog.catalogue_size import over_threshold
from catalog.do_not_contact import MissingSecret, UnusableIdentifier, digest
from catalog.hosting.directory import (
    ProviderRole,
    mark_rechecked,
    provider_roles,
    recheck_days,
    retire_channel,
    supersede_channel,
)
from catalog.hosting.services import speed_by_provider
from catalog.hosting.speed import SpeedStats, format_duration, takedown_speed
from catalog.media_shapes import trips_now
from catalog.models import (
    ComplaintChannel,
    Creator,
    CreatorSuppression,
    DirectoryProvider,
    DoNotContactRecord,
    HosterContact,
    HostingProvider,
    HostingRecord,
    HostRole,
    MediaUrlShape,
    ProviderFunctionOverride,
    ProviderIdentifier,
    RequestCount,
    RequestCountDaily,
    SafetyRuleSet,
    SafetyTerm,
    Site,
    SiteCatalogueSize,
    SiteHost,
    SiteObjection,
    SitePolicyChange,
    SiteRobotsPolicy,
    WatchlistEntry,
)
from catalog.request_accounting import (
    MEDIA_BUDGET,
    ORIGIN_BUDGET_KINDS,
    PAGE_BUDGET,
    window_start,
)
from catalog.request_report import report_lines
from catalog.services import do_not_contact_secret
from core.http import OUTCOME_REFUSED


class SiteHostInline(admin.TabularInline[SiteHost, Site]):
    """Host roles of a site (issue #74). Editing a row makes it the operator's decision."""

    model = SiteHost
    extra = 0
    fields = ("host", "role", "daily_request_budget", "source", "note")
    readonly_fields = ("source",)


class SiteAdminForm(ModelForm[Site]):
    """The Site change form, plus the one thing a policy change has to carry: a reason (issue #106).

    `policy_reason` is not a model field — it is written into the audit row the database trigger
    creates, through `catalog.policy_audit.policy_actor`. It is **required** when one of the
    legally weighty switches moves (`capture_play_click`, `age_confirmation`, `enabled`) or when
    a request budget is **raised**; lowering one, renaming a site or editing its notes needs none.
    """

    policy_reason = CharField(
        required=False,
        widget=Textarea(attrs={"rows": 2}),
        label="Reason for this policy change",
        help_text="Required when capture_play_click, age_confirmation or enabled changes, or a "
        "budget is raised. It is kept in the append-only Site policy change log, not on the site.",
    )

    class Meta:
        model = Site
        exclude = ()  # noqa: DJ006 - the admin decides the field list; this form only adds one

    def _moved(self, name: str) -> bool:
        """Did this field really change? Compared against the stored row, not `changed_data`.

        `changed_data` calls the *form field*'s `has_changed`, and for a JSONField an unedited `{}`
        re-submitted as the string `"{}"` reads as a change — which would demand a reason for
        saving a site nobody touched. The instance still holds the database values here: `clean()`
        runs before `_post_clean` writes the form onto it.
        """
        new = self.cleaned_data.get(name)
        old = getattr(self.instance, name, None)
        if isinstance(old, dict) or isinstance(new, dict):
            return (old or {}) != (new or {})
        return bool(old != new)

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if cleaned.get("policy_reason", "").strip():
            return cleaned
        weighty = [name for name in policy_audit.REASON_REQUIRED_FIELDS if self._moved(name)]
        weighty += [
            name
            for name in policy_audit.REASON_REQUIRED_ON_RAISE
            if self._moved(name)
            and (cleaned.get(name) or 0) > (getattr(self.instance, name, 0) or 0)
        ]
        if weighty:
            raise ValidationError(
                {
                    "policy_reason": "Say why: this change moves "
                    f"{', '.join(sorted(set(weighty)))}, which carries legal weight "
                    "(docs/legal/crawl-position.md, tripwire T4)."
                }
            )
        return cleaned


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin[Site]):
    list_display = (
        "domain",
        "name",
        "enabled",
        "crawl_delay_s",
        "robots_crawl_delay_s",
        "daily_request_budget",
        "origin_pages_24h",
        "daily_media_request_budget",
        "origin_media_24h",
        "paused_until",
        "age_confirmation_approved",
        "capture_enabled",
        "capture_play_click",
        "catalogue_share",
        "created_at",
    )
    form = SiteAdminForm
    readonly_fields = (
        "robots_crawl_delay_s",
        "pause_reason",
        "consecutive_failures",
        "request_usage",
        "robots_reservation",
        "policy_history",
    )
    list_filter = ("enabled", "capture_enabled", "capture_play_click")
    search_fields = ("domain", "name")
    inlines = (SiteHostInline,)

    def save_model(self, request: HttpRequest, obj: Site, form: Any, change: bool) -> None:
        """Save with the actor and reason the database trigger records (issue #106)."""
        reason = (form.cleaned_data.get("policy_reason") or "").strip()
        with policy_audit.policy_actor(f"admin:{request.user}", reason=reason):
            super().save_model(request, obj, form, change)

    def get_queryset(self, request: HttpRequest) -> QuerySet[Site]:
        since = window_start(timezone.now())

        def used(budget: str) -> Coalesce:
            usage = (
                RequestCount.objects.filter(
                    site=OuterRef("pk"),
                    role=HostRole.ORIGIN,
                    kind__in=ORIGIN_BUDGET_KINDS[budget],
                    hour__gte=since,
                )
                .exclude(outcome=OUTCOME_REFUSED)
                .order_by()
                .values("site")
                .annotate(total=Sum("requests"))
                .values("total")
            )
            return Coalesce(Subquery(usage), Value(0), output_field=BigIntegerField())

        return (
            super()
            .get_queryset(request)
            .select_related("catalogue_size")
            .annotate(pages_24h=used(PAGE_BUDGET), media_24h=used(MEDIA_BUDGET))
        )

    @staticmethod
    def _share(used: int, budget: int) -> str:
        return f"{used:,} ({used / budget:.0%})" if budget else f"{used:,}"

    @admin.display(description="Origin pages 24 h", ordering="pages_24h")
    def origin_pages_24h(self, obj: Site) -> str:
        return self._share(int(getattr(obj, "pages_24h", 0) or 0), obj.daily_request_budget)

    @admin.display(description="Origin media 24 h", ordering="media_24h")
    def origin_media_24h(self, obj: Site) -> str:
        return self._share(int(getattr(obj, "media_24h", 0) or 0), obj.daily_media_request_budget)

    @admin.display(description="Requests (origin vs CDN, 24 h and 7 d)")
    def request_usage(self, obj: Site) -> str:
        if obj.pk is None:
            return "-"
        lines = report_lines(timezone.now(), days=7, domain=obj.domain)
        return format_html("<pre>{}</pre>", "\n".join(lines))

    @admin.display(boolean=True, description="Age confirmation")
    def age_confirmation_approved(self, obj: Site) -> bool:
        return bool(obj.age_confirmation)

    @admin.display(description="Share of catalogue (ever / active)")
    def catalogue_share(self, obj: Site) -> str:
        """Our share of the site's own catalogue (issue #107). Unknown is shown as unknown."""
        record = getattr(obj, "catalogue_size", None)
        if record is None:
            return "total unknown"
        indexed, active = record.share_indexed, record.share_active
        if indexed is None or active is None:
            return "total unknown"
        flag = " over threshold" if over_threshold(record) else ""
        return f"{indexed:.1f} % / {active:.1f} %{flag}"

    @admin.display(description="robots.txt reservation (tripwire T2)")
    def robots_reservation(self, obj: Site) -> str:
        if obj.pk is None:
            return "-"
        lines = robots_policy.policy_lines(obj, timezone.now())
        return format_html("<pre>{}</pre>", "\n".join(lines)) if lines else "none declared"

    @admin.display(description="Policy switches changed (newest 20)")
    def policy_history(self, obj: Site) -> str:
        if obj.pk is None:
            return "-"
        rows = obj.policy_changes.all()[:20]
        if not rows:
            return "no change recorded"
        return format_html("<pre>{}</pre>", "\n".join(policy_audit.describe(row) for row in rows))

    def save_formset(self, request: HttpRequest, form: Any, formset: Any, change: bool) -> None:
        for host_form in formset.forms:
            instance = host_form.instance
            if isinstance(instance, SiteHost) and host_form.has_changed():
                instance.source = SiteHost.Source.OPERATOR
        super().save_formset(request, form, formset, change)


@admin.register(SiteHost)
class SiteHostAdmin(admin.ModelAdmin[SiteHost]):
    """Origin or CDN per host; a CDN host's own budget. Saved here = the operator's decision,
    which observation (`observe_site_hosts`) and `seed_sources` never overwrite."""

    list_display = ("host", "site", "role", "source", "daily_request_budget", "updated_at")
    list_filter = ("role", "source", "site")
    list_select_related = ("site",)
    search_fields = ("host", "site__domain")
    readonly_fields = ("source", "created_at", "updated_at")

    def save_model(self, request: HttpRequest, obj: SiteHost, form: Any, change: bool) -> None:
        obj.source = SiteHost.Source.OPERATOR
        super().save_model(request, obj, form, change)


@admin.register(SiteRobotsPolicy)
class SiteRobotsPolicyAdmin(admin.ModelAdmin[SiteRobotsPolicy]):
    """What each host's robots.txt reserves, and against whom (tripwire T2, issue #105).

    Written by the crawler from the robots.txt it already fetches. Read-only: the way to change what
    is here is the site's own file, and the way to end a reservation pause is the Site page.
    """

    list_display = (
        "host",
        "site",
        "names_us",
        "names_wildcard",
        "third_party_ai",
        "reserved_tokens",
        "headers",
        "first_seen_at",
        "changed_at",
        "checked_at",
    )
    list_filter = ("names_us", "names_wildcard", "third_party_ai", "site")
    list_select_related = ("site",)
    search_fields = ("host", "site__domain")
    readonly_fields = tuple(
        field.name for field in SiteRobotsPolicy._meta.fields if field.name != "id"
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.display(description="AI/TDM tokens disallowed")
    def reserved_tokens(self, obj: SiteRobotsPolicy) -> str:
        return ", ".join(obj.ai_tokens) or "-"

    @admin.display(description="reservation header(s)")
    def headers(self, obj: SiteRobotsPolicy) -> str:
        return ", ".join(obj.reservation_headers) or "-"


@admin.register(SitePolicyChange)
class SitePolicyChangeAdmin(admin.ModelAdmin[SitePolicyChange]):
    """The append-only record of every change to a site's policy switches (tripwire T4, #106).

    Written by a database trigger, for the admin, a command, a shell and a raw `.update()` alike.
    Nothing may add, change or delete a row here — the database refuses it.
    """

    list_display = ("changed_at", "site", "field", "old_value", "new_value", "actor", "reason")
    list_filter = ("field", "actor", "site")
    list_select_related = ("site",)
    search_fields = ("site__domain", "field", "actor", "reason")
    date_hierarchy = "changed_at"
    readonly_fields = tuple(
        field.name for field in SitePolicyChange._meta.fields if field.name != "id"
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: SitePolicyChange | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: SitePolicyChange | None = None
    ) -> bool:
        return False


@admin.register(MediaUrlShape)
class MediaUrlShapeAdmin(admin.ModelAdmin[MediaUrlShape]):
    """What each media URL shape has been answering per site, and whether it is suppressed
    (ADR 0041). Written by the media worker; the operator's one action here is deleting a row,
    which clears the breaker for that shape — it then re-learns whatever is true."""

    list_display = (
        "shape",
        "site",
        "state",
        "gone_streak",
        "recent_share",
        "gone_total",
        "ok_total",
        "trips",
        "last_gone_at",
        "last_ok_at",
    )
    list_filter = ("site",)
    list_select_related = ("site",)
    search_fields = ("shape", "site__domain")
    ordering = ("-gone_total", "shape")
    readonly_fields = tuple(
        field.name for field in MediaUrlShape._meta.fields if field.name != "id"
    )

    @admin.display(description="state")
    def state(self, obj: MediaUrlShape) -> str:
        if obj.suppressed_until is None:
            return "would be suppressed" if trips_now(obj, timezone.now()) else "-"
        if timezone.now() < obj.suppressed_until:
            return f"suppressed until {obj.suppressed_until:%Y-%m-%d %H:%M} UTC"
        return "suppressed, probing"

    @admin.display(description="recent gone")
    def recent_share(self, obj: MediaUrlShape) -> str:
        settled = obj.gone_recent + obj.ok_recent
        return f"{obj.gone_recent / settled:.0%} of {settled}" if settled else "-"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: MediaUrlShape | None = None) -> bool:
        return False


@admin.register(RequestCount)
class RequestCountAdmin(admin.ModelAdmin[RequestCount]):
    """Hourly request counters (issue #74), written by the request accounting: read-only. The
    per-site summary is on the Site page and in `manage.py request_report`."""

    list_display = ("hour", "site", "host", "role", "request_type", "outcome", "requests", "bytes")
    list_filter = ("role", "request_type", "outcome", "site")
    list_select_related = ("site",)
    search_fields = ("host", "site__domain")
    date_hierarchy = "hour"
    ordering = ("-hour", "host")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: RequestCount | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: RequestCount | None = None) -> bool:
        return False


@admin.register(RequestCountDaily)
class RequestCountDailyAdmin(admin.ModelAdmin[RequestCountDaily]):
    """Daily request counters rolled up from hourly rows past their retention: read-only."""

    list_display = ("day", "site", "host", "role", "request_type", "outcome", "requests", "bytes")
    list_filter = ("role", "request_type", "outcome", "site")
    list_select_related = ("site",)
    search_fields = ("host", "site__domain")
    date_hierarchy = "day"
    ordering = ("-day", "host")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: RequestCountDaily | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: RequestCountDaily | None = None
    ) -> bool:
        return False


@admin.register(Creator)
class CreatorAdmin(admin.ModelAdmin[Creator]):
    list_display = ("name", "aliases", "needs_review", "is_suppressed", "created_at")
    list_filter = ("needs_review",)
    search_fields = ("name",)
    actions = ("mark_reviewed",)
    # `Creator.suppressed` reads the reverse one-to-one, so without this the Suppressed column
    # costs one query per row of the page (38 of the changelist's 43 queries when measured).
    list_select_related = ("suppression",)

    @admin.display(boolean=True, description="Suppressed")
    def is_suppressed(self, obj: Creator) -> bool:
        """Index suppression (docs/suppression.md): applied and lifted by `suppress_creator`."""
        return obj.suppressed

    @admin.action(description="Mark reviewed (becomes crawlable like any creator)")
    def mark_reviewed(self, request: HttpRequest, queryset: QuerySet[Creator]) -> None:
        """Names customers typed (issue #29) stay out of crawling until a human checked them.

        Merge a duplicate into the existing creator first (move the originals' creators), then
        delete it; mark only genuinely new creators reviewed.
        """
        updated = queryset.filter(needs_review=True).update(needs_review=False)
        self.message_user(request, f"{updated} creator(s) marked reviewed.")


@admin.register(SafetyTerm)
class SafetyTermAdmin(admin.ModelAdmin[SafetyTerm]):
    list_display = ("term", "is_regex", "category", "action", "enabled", "created_at")
    list_editable = ("enabled",)
    list_filter = ("action", "category", "enabled", "is_regex")
    search_fields = ("term",)


@admin.register(SafetyRuleSet)
class SafetyRuleSetAdmin(admin.ModelAdmin[SafetyRuleSet]):
    """Snapshots of the safety rule set a recorded decision names (docs/safety.md). Read-only."""

    list_display = ("version", "created_at")
    readonly_fields = ("version", "terms", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(HostingProvider)
class HostingProviderAdmin(admin.ModelAdmin[HostingProvider]):
    """Written by `lookup_hosting`; read-only here (ADR 0027).

    The reaction statistics below, and the speed band, are **internal**: a customer sees only a
    rounded response history of the providers attached to their own findings (ADR 0027 amendment
    2026-09-17, `catalog.hosting.history`), never these numbers.
    """

    list_display = ("asn", "name", "country", "updated_at")
    search_fields = ("name",)
    readonly_fields = (
        "takedown_speed_band",
        "responded",
        "median_to_response",
        "p90_to_response",
        "removals",
        "median_to_removal",
        "p90_to_removal",
    )

    @staticmethod
    def _speed(obj: HostingProvider) -> SpeedStats:
        cached: SpeedStats | None = getattr(obj, "_speed_stats", None)
        if cached is None:
            cached = speed_by_provider([obj.pk], timezone.now()).get(obj.pk, SpeedStats())
            obj._speed_stats = cached  # type: ignore[attr-defined]
        return cached

    @admin.display(description="Takedown speed (internal band)")
    def takedown_speed_band(self, obj: HostingProvider) -> str:
        return takedown_speed(self._speed(obj))

    @admin.display(description="Responded (of notices old enough to judge)")
    def responded(self, obj: HostingProvider) -> str:
        stats = self._speed(obj)
        share = stats.share_responded
        if share is None:
            return "-"
        return f"{stats.responded} of {stats.response_samples} ({share:.0%})"

    @admin.display(description="Median time to first response")
    def median_to_response(self, obj: HostingProvider) -> str:
        return format_duration(self._speed(obj).median_to_response)

    @admin.display(description="p90 time to first response")
    def p90_to_response(self, obj: HostingProvider) -> str:
        return format_duration(self._speed(obj).p90_to_response)

    @admin.display(description="Removed (of notices old enough to judge)")
    def removals(self, obj: HostingProvider) -> str:
        stats = self._speed(obj)
        if not stats.removal_samples:
            return "-"
        return f"{stats.removals_timed} of {stats.removal_samples}"

    @admin.display(description="Median time to removal (observed, about a day precise)")
    def median_to_removal(self, obj: HostingProvider) -> str:
        return format_duration(self._speed(obj).median_to_removal)

    @admin.display(description="p90 time to removal")
    def p90_to_removal(self, obj: HostingProvider) -> str:
        return format_duration(self._speed(obj).p90_to_removal)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: HostingProvider | None = None
    ) -> bool:
        return False


@admin.register(HostingRecord)
class HostingRecordAdmin(admin.ModelAdmin[HostingRecord]):
    """Who hosts a hostname and whom to complain to. Written by `lookup_hosting`; read-only."""

    list_display = (
        "hostname",
        "status",
        "country",
        "asn",
        "cdn",
        "abuse_email",
        "abuse_source",
        "registrar_name",
        "looked_up_at",
    )
    list_filter = ("status", "abuse_source", "cdn", "country")
    list_select_related = ("provider",)
    search_fields = ("hostname", "as_name", "organisation", "registrar_name")
    readonly_fields = ("providers_and_channels",)

    @admin.display(description="Providers, functions and suggested notice channels")
    def providers_and_channels(self, obj: HostingRecord) -> str:
        """Staff-only view of the provider directory for this host (ADR 0027, 2026-09-17)."""
        roles = provider_roles([obj.hostname], timezone.now())[obj.hostname]
        if not roles:
            return "-"
        return format_html_join(
            "",
            "<p><strong>{}</strong>: {} &middot; country {} &middot; nexus {} ({}) &middot; "
            "function {} ({}){}<br>suggested channel: {}</p>",
            (_role_row(role) for role in roles),
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: HostingRecord | None = None) -> bool:
        return False


def _role_row(role: ProviderRole) -> tuple[str, ...]:
    suggested = role.suggested
    channel = "none known"
    if suggested is not None:
        channel = (
            f"{suggested.kind} {suggested.email or suggested.url} "
            f"[{suggested.support}, from {suggested.source}, transfer {suggested.transfer_basis}"
            f"{', re-check due' if suggested.recheck_due else ''}]"
        )
    note = f" - {role.function_note}" if role.function_note else ""
    return (
        role.slot,
        role.provider_name or "-",
        role.country or "-",
        role.nexus,
        role.nexus_source,
        role.function,
        role.function_source,
        note,
        channel,
    )


@admin.register(HosterContact)
class HosterContactAdmin(admin.ModelAdmin[HosterContact]):
    """The raw cooperation history: internal only. Customers see a takedown-chance band, never
    these rows, counts or response times (ADR 0027). Nothing writes them until notices exist."""

    list_display = (
        "provider",
        "hostname",
        "channel",
        "sent_at",
        "responded_at",
        "removed_observed_at",
        "outcome",
    )
    readonly_fields = ("removed_observed_at",)
    list_filter = ("outcome", "channel")
    list_select_related = ("provider",)
    raw_id_fields = ("provider",)
    search_fields = ("hostname", "provider__name")


@admin.register(WatchlistEntry)
class WatchlistEntryAdmin(admin.ModelAdmin[WatchlistEntry]):
    """Creators noted without an Auftrag (issue #71). Not crawled unless `crawl` is ticked; the
    breadcrumbs admin shows where they already are. Removing an entry deletes nothing indexed."""

    list_display = ("creator", "purpose", "crawl", "note", "added_at")
    list_filter = ("purpose", "crawl")
    list_select_related = ("creator",)
    raw_id_fields = ("creator",)
    search_fields = ("creator__name", "note")
    readonly_fields = ("added_at",)


#: What the screen says about itself. Both markers are easy to confuse, so each names the other.
DO_NOT_CONTACT_DESCRIPTION = (
    "A person or company told us not to contact them again. This record exists so that we can "
    "keep that promise; it is not a prospect file, so no findings, no counts and no site name go "
    "in the note. "
    "<br><b>This is not index suppression.</b> Someone who also wants their name out of the index "
    "needs <i>Creator suppressions</i> as well (docs/suppression.md); neither follows from the "
    "other."
)
#: In form order. `identifier` is the write-only field the form adds; it is dropped once the row
#: exists, because the hash cannot give it back.
DO_NOT_CONTACT_FIELDS = (
    "identifier",
    "identifier_kind",
    "received_on",
    "channel",
    "recorded_by",
    "creator",
    "note",
    "created_at",
)


class DoNotContactRecordForm(ModelForm[DoNotContactRecord]):
    """Record one "no". The identifier is typed once, hashed, and never stored.

    It is a form field and not a model field on purpose: nothing can save it by accident, and the
    admin's own change log records the field *name*, never what was typed.
    """

    identifier = CharField(
        max_length=500,
        widget=TextInput(attrs={"autocomplete": "off", "size": "60"}),
        help_text="The address, name, handle or number the 'no' covers, as it was given. It is "
        "hashed with the server key and never stored — you cannot look it up again, so record "
        "every spelling you were given (a number nationally and internationally, an address and "
        "the company name).",
    )

    class Meta:
        model = DoNotContactRecord
        fields = ("identifier_kind", "received_on", "channel", "recorded_by", "creator", "note")

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        kind, raw = cleaned.get("identifier_kind"), cleaned.get("identifier")
        if not kind or not raw:
            return cleaned
        try:
            hashed = digest(kind, raw, do_not_contact_secret())
        except MissingSecret as exc:
            raise ValidationError(str(exc)) from exc
        except UnusableIdentifier as exc:
            self.add_error("identifier", str(exc))
            return cleaned
        existing = DoNotContactRecord.objects.filter(identifier_hash=hashed).first()
        if existing is not None and existing.pk != self.instance.pk:
            self.add_error(
                "identifier",
                f"already recorded on {existing.received_on} — nothing to do, and nothing is "
                "changed by recording it twice.",
            )
            return cleaned
        self.instance.identifier_hash = hashed
        return cleaned


@admin.register(DoNotContactRecord)
class DoNotContactRecordAdmin(admin.ModelAdmin[DoNotContactRecord]):
    """Identifiers we must never contact again (issue #98, docs/do-not-contact.md).

    One row per identifier, holding a keyed hash of it and nothing else that could name anybody.
    Adding a row is how a "no" is recorded; checking before writing to someone is
    `manage.py may_contact`, which answers by position and never echoes an identifier.

    Nothing here is deletable, and the hash is never displayed: there is nothing to copy out, and
    the objection has to outlive the campaign that caused it.
    """

    form = DoNotContactRecordForm
    list_display = ("identifier_kind", "received_on", "channel", "recorded_by", "creator", "note")
    list_filter = ("identifier_kind", "channel")
    list_select_related = ("creator",)
    raw_id_fields = ("creator",)
    date_hierarchy = "received_on"
    readonly_fields = ("created_at",)
    fieldsets = (
        (None, {"description": DO_NOT_CONTACT_DESCRIPTION, "fields": DO_NOT_CONTACT_FIELDS}),
    )

    def get_form(
        self,
        request: HttpRequest,
        obj: DoNotContactRecord | None = None,
        change: bool = False,
        **kwargs: Any,
    ) -> Any:
        form = super().get_form(request, obj, change=change, **kwargs)
        if obj is None and "recorded_by" in form.base_fields:
            form.base_fields["recorded_by"].initial = request.user.get_username()
        return form

    def get_fieldsets(self, request: HttpRequest, obj: DoNotContactRecord | None = None) -> Any:
        if obj is None:
            return super().get_fieldsets(request, obj)
        # The identifier is gone for good once it is hashed, so an existing row can neither show
        # nor re-take it. Only the staff note stays editable.
        fields = tuple(name for name in DO_NOT_CONTACT_FIELDS if name != "identifier")
        return ((None, {"description": DO_NOT_CONTACT_DESCRIPTION, "fields": fields}),)

    def get_readonly_fields(
        self, request: HttpRequest, obj: DoNotContactRecord | None = None
    ) -> tuple[str, ...]:
        if obj is None:
            return ("created_at",)
        return ("identifier_kind", "received_on", "channel", "recorded_by", "creator", "created_at")

    def has_delete_permission(
        self, request: HttpRequest, obj: DoNotContactRecord | None = None
    ) -> bool:
        return False


@admin.register(CreatorSuppression)
class CreatorSuppressionAdmin(admin.ModelAdmin[CreatorSuppression]):
    """Names we must not index (docs/suppression.md). Read-only except the answer date.

    Applying and lifting a suppression deletes and keeps carefully chosen things, so it goes
    through `manage.py suppress_creator` (dry run first), never through this screen. What staff
    do record here is when the requester was answered — the one fact that arrives later.
    Nothing is deletable: the row is the record that keeps the name out of the index.
    """

    list_display = (
        "creator",
        "basis",
        "requested_on",
        "answer_due_on",
        "answered_on",
        "identity_check",
        "applied_at",
        "lifted_at",
    )
    list_filter = ("basis", "identity_check")
    list_select_related = ("creator",)
    raw_id_fields = ("creator",)
    search_fields = ("creator__name",)
    readonly_fields = (
        "creator",
        "basis",
        "requested_on",
        "answer_due_on",
        "identity_check",
        "identity_note",
        "decided_by",
        "applied_at",
        "removed_page_links",
        "removed_breadcrumbs",
        "redacted_pages",
        "lifted_at",
        "lift_reason",
        "created_at",
        "updated_at",
        "the_other_marker",
    )

    @admin.display(description="also")
    def the_other_marker(self, obj: CreatorSuppression) -> str:
        """The two markers answer two different sentences (issue #98, docs/do-not-contact.md)."""
        return (
            "This keeps the name out of the index. It does not stop us writing to anybody: "
            "someone who also said 'do not contact me' needs a Do-not-contact record as well."
        )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: CreatorSuppression | None = None
    ) -> bool:
        return False


# --- Provider directory (ADR 0027 amendment 2026-09-17): staff-maintained, no notice is sent. ---


def _due_before() -> datetime:
    return timezone.now() - timedelta(days=recheck_days())


class ProviderIdentifierInline(admin.TabularInline[ProviderIdentifier, DirectoryProvider]):
    """AS numbers, registrar IANA ids and hostnames that lead a lookup to this provider."""

    model = ProviderIdentifier
    extra = 1
    fields = ("kind", "value")


class CurrentChannelInline(admin.TabularInline[ComplaintChannel, DirectoryProvider]):
    """Current channel versions, read-only here: a change goes through the channel admin, which
    keeps the old version."""

    model = ComplaintChannel
    extra = 0
    fields = ("kind", "url", "email", "transfer_basis", "checked_at")
    readonly_fields = fields
    show_change_link = True
    verbose_name_plural = "current complaint channels (edit via Complaint channels)"

    def get_queryset(self, request: HttpRequest) -> QuerySet[ComplaintChannel]:
        return super().get_queryset(request).filter(superseded_at__isnull=True)

    def has_add_permission(
        self, request: HttpRequest, obj: DirectoryProvider | None = None
    ) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: DirectoryProvider | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: DirectoryProvider | None = None
    ) -> bool:
        return False


def _warn_due_channels(request: HttpRequest) -> None:
    due = ComplaintChannel.objects.filter(
        superseded_at__isnull=True, checked_at__lte=_due_before()
    ).count()
    if due:
        messages.warning(
            request,
            f"{due} complaint channel(s) were last verified more than {recheck_days()} days ago "
            "and are due for re-checking (filter: re-check due).",
        )


@admin.register(DirectoryProvider)
class DirectoryProviderAdmin(admin.ModelAdmin[DirectoryProvider]):
    """The provider directory. Organisations and role addresses only, never a named person."""

    list_display = ("name", "establishment_country", "nexus", "function", "updated_at")
    list_filter = ("nexus", "function")
    search_fields = ("name", "identifiers__value")
    inlines = (ProviderIdentifierInline, CurrentChannelInline)

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        _warn_due_channels(request)
        return super().changelist_view(request, extra_context)


class ChannelVersionFilter(admin.SimpleListFilter):
    title = "version"
    parameter_name = "version"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin[ComplaintChannel]
    ) -> tuple[tuple[str, str], ...]:
        return (("current", "Current"), ("superseded", "Superseded"))

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[ComplaintChannel]
    ) -> QuerySet[ComplaintChannel]:
        if self.value() == "current":
            return queryset.filter(superseded_at__isnull=True)
        if self.value() == "superseded":
            return queryset.filter(superseded_at__isnull=False)
        return queryset


class RecheckDueFilter(admin.SimpleListFilter):
    title = "re-check due"
    parameter_name = "recheck_due"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin[ComplaintChannel]
    ) -> tuple[tuple[str, str], ...]:
        return (("yes", "Yes"),)

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[ComplaintChannel]
    ) -> QuerySet[ComplaintChannel]:
        if self.value() == "yes":
            return queryset.filter(superseded_at__isnull=True, checked_at__lte=_due_before())
        return queryset


@admin.register(ComplaintChannel)
class ComplaintChannelAdmin(admin.ModelAdmin[ComplaintChannel]):
    """Complaint channels, **versioned**: saving a change writes a new current version and keeps
    the old one (superseded, read-only). Nothing is deleted; a channel that is gone is retired."""

    list_display = (
        "provider",
        "kind",
        "url",
        "email",
        "transfer_basis",
        "checked_at",
        "recheck_due",
        "is_current",
    )
    list_filter = (ChannelVersionFilter, RecheckDueFilter, "kind", "transfer_basis")
    list_select_related = ("provider",)
    search_fields = ("provider__name", "url", "email")
    autocomplete_fields = ("provider",)
    readonly_fields = ("created_at", "superseded_at", "superseded_by")
    actions = ("mark_rechecked_now", "retire")

    @admin.display(boolean=True, description="Current")
    def is_current(self, obj: ComplaintChannel) -> bool:
        return obj.superseded_at is None

    @admin.display(boolean=True, description="Re-check due")
    def recheck_due(self, obj: ComplaintChannel) -> bool:
        return obj.superseded_at is None and obj.checked_at <= _due_before()

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        _warn_due_channels(request)
        return super().changelist_view(request, extra_context)

    def has_change_permission(
        self, request: HttpRequest, obj: ComplaintChannel | None = None
    ) -> bool:
        if obj is not None and obj.superseded_at is not None:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(
        self, request: HttpRequest, obj: ComplaintChannel | None = None
    ) -> bool:
        return False

    def save_model(
        self,
        request: HttpRequest,
        obj: ComplaintChannel,
        form: ModelForm[ComplaintChannel],
        change: bool,
    ) -> None:
        """A change becomes a new version; the edited row itself is only stamped superseded."""
        if change and obj.pk is not None:
            if form.has_changed():
                supersede_channel(obj.pk, obj, timezone.now())
            return
        super().save_model(request, obj, form, change)

    @admin.action(description="Mark verified now (no change): new version checked today")
    def mark_rechecked_now(
        self, request: HttpRequest, queryset: QuerySet[ComplaintChannel]
    ) -> None:
        now = timezone.now()
        channels = list(queryset.filter(superseded_at__isnull=True))
        for channel in channels:
            mark_rechecked(channel, now)
        self.message_user(request, f"{len(channels)} channel(s) marked verified.", messages.SUCCESS)

    @admin.action(description="Retire (the channel no longer exists)")
    def retire(self, request: HttpRequest, queryset: QuerySet[ComplaintChannel]) -> None:
        now = timezone.now()
        channels = list(queryset.filter(superseded_at__isnull=True))
        for channel in channels:
            retire_channel(channel, now)
        self.message_user(request, f"{len(channels)} channel(s) retired.", messages.SUCCESS)


@admin.register(ProviderFunctionOverride)
class ProviderFunctionOverrideAdmin(admin.ModelAdmin[ProviderFunctionOverride]):
    """A person's decision about a provider's function for one hostname. A note is required; the
    only way to call a CDN `storing_cdn`."""

    list_display = ("hostname", "slot", "function", "note", "updated_at")
    list_filter = ("slot", "function")
    search_fields = ("hostname", "note")


@admin.register(SiteCatalogueSize)
class SiteCatalogueSizeAdmin(admin.ModelAdmin[SiteCatalogueSize]):
    """The denominator of our catalogue share per site (issue #107, ADR 0049).

    Editable by hand — that is how the sitemap figure of a reconnaissance note gets in without
    anyone fetching a sitemap. The page counts and `counted_at` are written by
    `index.catalogue_share` and are read-only here; `notified_at` is the alert bookkeeping.
    """

    list_display = (
        "site",
        "total",
        "method",
        "source_path",
        "observed_at",
        "pages_indexed",
        "pages_active",
        "share_ever",
        "share_active",
        "counted_at",
    )
    list_filter = ("method",)
    list_select_related = ("site",)
    search_fields = ("site__domain", "source_path", "note")
    readonly_fields = (
        "pages_indexed",
        "pages_active",
        "counted_at",
        "notified_at",
        "first_recorded_at",
    )

    @admin.display(description="Share ever indexed")
    def share_ever(self, obj: SiteCatalogueSize) -> str:
        share = obj.share_indexed
        return "—" if share is None else f"{share:.1f} %"

    @admin.display(description="Share active")
    def share_active(self, obj: SiteCatalogueSize) -> str:
        share = obj.share_active
        return "—" if share is None else f"{share:.1f} %"


@admin.register(SiteObjection)
class SiteObjectionAdmin(admin.ModelAdmin[SiteObjection]):
    """A site operator's objection and what we did about it (issue #108, ADR 0050).

    **A record, so it cannot be deleted** — like `DoNotContactRecord`: the point of writing down
    who asked us to stop is that the answer survives the person who gave it. The action below is
    the only way a decision reaches the site's switches (`catalog.site_objections`); editing
    `Site.enabled` by hand instead is exactly the state this record exists to end.

    Procedure, including what never goes in the note: `docs/runbooks/site-objection.md`.
    """

    list_display = (
        "site",
        "received_on",
        "kind",
        "channel",
        "decision",
        "decided_by",
        "decided_at",
        "applied_at",
    )
    list_filter = ("decision", "kind", "channel")
    list_select_related = ("site",)
    search_fields = ("site__domain", "note", "decision_reason")
    readonly_fields = ("applied_at", "created_at", "updated_at")
    actions = ("apply_decision",)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    @admin.action(description="Apply the recorded decision to the site's switches")
    def apply_decision(self, request: HttpRequest, queryset: QuerySet[SiteObjection]) -> None:
        applied = 0
        for objection in queryset.select_related("site"):
            changes = site_objections.apply_decision(
                objection, actor=f"admin:{request.user}", now=timezone.now()
            )
            if changes:
                applied += 1
                self.message_user(
                    request,
                    f"{objection.site.domain}: "
                    + ", ".join(f"{field} -> {value}" for field, value in sorted(changes.items())),
                    messages.SUCCESS,
                )
        if not applied:
            self.message_user(
                request,
                "Nothing to apply: a pending or declined objection changes no switch.",
                messages.INFO,
            )
