"""Staff side of notice drafts (issue #33, ADR 0035): read-only.

Every write goes through `marceline.notices.services`, driven by the customer screen; staff never
edit a draft here. This is the audit view: what was drafted, what the customer confirmed, exactly
what text was approved, and the append-only event log.
"""

from typing import Any

from django.contrib import admin

from marceline.notices.models import (
    NoticeConfirmation,
    NoticeDraft,
    NoticeEvent,
    NoticeFinding,
)
from marceline.rights.admin import ReadOnlyAdmin


class NoticeFindingInline(admin.TabularInline[NoticeFinding, NoticeDraft]):
    model = NoticeFinding
    fields = (
        "page",
        "url_snapshot",
        "availability_snapshot",
        "extent_snapshot",
        "requires_exception_review",
        "exception_confirmed",
        "included",
    )
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:
        return False


class NoticeConfirmationInline(admin.TabularInline[NoticeConfirmation, NoticeDraft]):
    model = NoticeConfirmation
    fields = ("kind", "version", "language", "confirmed_at", "confirmed_by_label")
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:
        return False


class NoticeEventInline(admin.TabularInline[NoticeEvent, NoticeDraft]):
    model = NoticeEvent
    fields = ("at", "kind", "actor_kind", "actor_label", "detail")
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:
        return False


@admin.register(NoticeDraft)
class NoticeDraftAdmin(ReadOnlyAdmin):
    list_display = (
        "pk",
        "public_id",
        "organisation",
        "is_sandbox",
        "status",
        "rendered_variant",
        "recipient_hostname",
        "language",
        "created_at",
    )
    list_filter = ("status", "rendered_variant", "is_sandbox", "language")
    list_select_related = ("organisation",)
    search_fields = (
        "public_id",
        "organisation__name",
        "recipient_hostname",
        "work_title",
        "sha256",
    )
    inlines = (NoticeFindingInline, NoticeConfirmationInline, NoticeEventInline)
    readonly_fields = tuple(field.name for field in NoticeDraft._meta.fields)


@admin.register(NoticeEvent)
class NoticeEventAdmin(ReadOnlyAdmin):
    list_display = ("at", "draft", "kind", "actor_kind", "actor_label")
    list_filter = ("kind", "actor_kind")
    list_select_related = ("draft",)
    raw_id_fields = ("draft",)
