"""Ops views of the production cache (ADR 0028). The admin only exists on development, so what is
useful here is the development side: service tokens (revoke), the purge outbox (what is stuck) and
the customer writes pulled from production (what failed or is quarantined).
"""

from django.contrib import admin
from django.http import HttpRequest

from delivery.models import InboxItem, PurgeOutbox, ServiceToken


@admin.register(ServiceToken)
class ServiceTokenAdmin(admin.ModelAdmin[ServiceToken]):
    list_display = ("name", "scope", "created_at", "revoked_at")
    readonly_fields = ("name", "scope", "digest", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        # Tokens are created with `manage.py create_service_token`, which shows the token once.
        return False


@admin.register(PurgeOutbox)
class PurgeOutboxAdmin(admin.ModelAdmin[PurgeOutbox]):
    list_display = ("pk", "mode", "reason", "created_at", "attempts", "last_error", "sent_at")
    list_filter = ("mode", "reason")
    readonly_fields = (
        "mode",
        "organisation_ids",
        "page_ids",
        "reason",
        "created_at",
        "attempts",
        "last_attempt_at",
        "last_error",
        "sent_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(InboxItem)
class InboxItemAdmin(admin.ModelAdmin[InboxItem]):
    """Customer writes development pulled from production (ADR 0028 amendment, write path).
    Read-only: a quarantined write is applied again with `pull_customer_writes --retry ITEM`."""

    list_display = (
        "item_id",
        "kind",
        "organisation_id",
        "status",
        "result_code",
        "reason",
        "attempts",
        "settled_at",
        "acknowledged_at",
    )
    list_filter = ("status", "kind")
    search_fields = ("item_id",)
    exclude = ("payload",)
    readonly_fields = (
        "item_id",
        "organisation_id",
        "user_id",
        "kind",
        "received_at",
        "status",
        "result_code",
        "original_id",
        "attempts",
        "reason",
        "first_pulled_at",
        "settled_at",
        "acknowledged_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: InboxItem | None = None) -> bool:
        return False
