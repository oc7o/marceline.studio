from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from core.models import HostSlot, JobRun


@admin.register(JobRun)
class JobRunAdmin(admin.ModelAdmin[JobRun]):
    list_display = (
        "name",
        "last_status",
        "last_started_at",
        "last_finished_at",
        "last_succeeded_at",
        "last_message",
    )
    list_filter = ("last_status",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: JobRun | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: JobRun | None = None) -> bool:
        return False


@admin.register(HostSlot)
class HostSlotAdmin(admin.ModelAdmin[HostSlot]):
    """Host slots of the crawl runtime (ADR 0030). Only the operator floor is editable."""

    list_display = (
        "key",
        "next_allowed_at",
        "last_delay_s",
        "min_delay_s",
        "leased_until",
        "last_lane",
        "waiting_lane",
        "penalty_until",
        "penalty_reason",
    )
    list_filter = ("last_lane",)
    search_fields = ("key",)
    fields = (
        "key",
        "min_delay_s",
        "next_allowed_at",
        "last_started_at",
        "last_delay_s",
        "leased_until",
        "lease_owner",
        "last_lane",
        "waiting_lane",
        "waiting_until",
        "penalty_until",
        "penalty_reason",
    )
    readonly_fields = tuple(name for name in fields if name != "min_delay_s")
    actions = ("clear_penalty",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def save_model(self, request: HttpRequest, obj: HostSlot, form: object, change: bool) -> None:
        # Write the floor only: the other columns change several times a second, and saving the
        # form's stale copy of them would hand out a slot early.
        HostSlot.objects.filter(pk=obj.pk).update(min_delay_s=obj.min_delay_s)

    @admin.action(description="Clear penalty")
    def clear_penalty(self, request: HttpRequest, queryset: QuerySet[HostSlot]) -> None:
        queryset.update(penalty_until=None, penalty_reason="")
