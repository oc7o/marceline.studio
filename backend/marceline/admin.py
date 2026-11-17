"""Internal ops UI for who owns an original (ADR 0031). Customers never see this.

Every write goes through `marceline.app.ownership.reassign`, so the admin moves exactly what the
`assign_originals` command would: the named original and every reference of the same file, each
move logged. Moving an original away from an organisation needs an explicit confirmation tick —
its case file goes with it, and the customer who had it stops seeing it at once.
"""

from typing import Any

from django import forms
from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from marceline.app.ownership import Via, owner_of, reassign, with_twins
from marceline.app.rights import text_of
from marceline.deletion import admin as deletion_admin  # noqa: F401  (registers the marks admin)
from marceline.models import (
    CreatorMergeLog,
    OriginalOwnership,
    OriginalPublicId,
    RefusedClaim,
    RightsConfirmation,
)
from marceline.notices import admin as notices_admin  # noqa: F401  (registers the notices admin)
from marceline.rights import admin as rights_admin  # noqa: F401  (registers the rights admin)


class OriginalOwnershipForm(forms.ModelForm[OriginalOwnership]):
    confirm_move = forms.BooleanField(
        required=False,
        label="Yes, move it",
        help_text="Required when an original (or another reference of the same file) already "
        "belongs to a different organisation: that customer loses it and its case file.",
    )

    class Meta:
        model = OriginalOwnership
        fields = ("reference", "organisation")

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        reference = cleaned.get("reference") or getattr(self.instance, "reference", None)
        organisation = cleaned.get("organisation")
        if reference is None or organisation is None:
            return cleaned
        takes_from = sorted(
            {
                owner.name
                for twin in with_twins([reference.pk])
                if (owner := owner_of(twin.pk)) is not None and owner.pk != organisation.pk
            }
        )
        if takes_from and not cleaned.get("confirm_move"):
            raise forms.ValidationError(
                f"This moves the original away from {', '.join(takes_from)}. "
                "Tick “Yes, move it” to confirm."
            )
        return cleaned


@admin.register(OriginalOwnership)
class OriginalOwnershipAdmin(admin.ModelAdmin[OriginalOwnership]):
    form = OriginalOwnershipForm
    list_display = ("reference", "organisation", "assigned_via", "assigned_by", "assigned_at")
    list_filter = ("organisation", "assigned_via")
    list_select_related = ("reference", "organisation", "assigned_by")
    search_fields = ("reference__title", "organisation__name")
    raw_id_fields = ("reference",)
    readonly_fields = ("assigned_via", "assigned_by", "assigned_at")

    def get_readonly_fields(
        self, request: HttpRequest, obj: OriginalOwnership | None = None
    ) -> tuple[str, ...]:
        # The original is the row's identity; to assign another one, add a row for it.
        base = tuple(self.readonly_fields)
        return ("reference", *base) if obj is not None else base

    def get_fields(
        self, request: HttpRequest, obj: OriginalOwnership | None = None
    ) -> tuple[str, ...]:
        return ("reference", "organisation", "confirm_move", *self.readonly_fields)

    def save_model(
        self,
        request: HttpRequest,
        obj: OriginalOwnership,
        form: forms.ModelForm[OriginalOwnership],
        change: bool,
    ) -> None:
        user = request.user if request.user.is_authenticated else None
        reassign(
            with_twins([obj.reference_id]),
            obj.organisation,
            via=Via.ADMIN,
            by=user,
        )

    def delete_model(self, request: HttpRequest, obj: OriginalOwnership) -> None:
        self.delete_queryset(request, OriginalOwnership.objects.filter(pk=obj.pk))

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[OriginalOwnership]) -> None:
        """Unassigning goes through `reassign` too, twins included, and is logged."""
        user = request.user if request.user.is_authenticated else None
        ids = list(queryset.values_list("reference_id", flat=True))
        reassign(with_twins(ids), None, via=Via.ADMIN, by=user)


@admin.register(OriginalPublicId)
class OriginalPublicIdAdmin(admin.ModelAdmin[OriginalPublicId]):
    """Which file a customer's `orig_…` id names (ADR 0033). Read-only: ids are never edited."""

    list_display = ("public_id", "organisation", "sha256", "created_at")
    list_filter = ("organisation",)
    list_select_related = ("organisation",)
    search_fields = ("=public_id", "=sha256", "organisation__name")
    readonly_fields = ("public_id", "organisation", "sha256", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: OriginalPublicId | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: OriginalPublicId | None = None
    ) -> bool:
        return False


@admin.register(RightsConfirmation)
class RightsConfirmationAdmin(admin.ModelAdmin[RightsConfirmation]):
    """What customers declared when adding an original in the app (issue #29). Audit data:
    read-only, never edited or added by hand."""

    list_display = (
        "reference",
        "organisation",
        "confirmed_by",
        "text_version",
        "language",
        "confirmed_at",
    )
    list_filter = ("text_version", "language")
    list_select_related = ("reference", "organisation", "confirmed_by")
    raw_id_fields = ("reference",)
    readonly_fields = (
        "reference",
        "organisation",
        "confirmed_by",
        "text_version",
        "language",
        "wording",
        "confirmed_at",
    )

    @admin.display(description="Wording the customer ticked")
    def wording(self, obj: RightsConfirmation) -> str:
        return text_of(obj.text_version, obj.language) or "unknown version"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: RightsConfirmation | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: RightsConfirmation | None = None
    ) -> bool:
        return False


@admin.register(RefusedClaim)
class RefusedClaimAdmin(admin.ModelAdmin[RefusedClaim]):
    """A customer upload refused with the neutral 409 (issue #29 amendment, operator 2026-09-17).

    Staff work these from `marceline/review/claims.py`'s queue in the SPA; the admin is a
    read-only mirror. The customer never sees this row or anything derived from it."""

    list_display = ("organisation", "sha256", "reason", "status", "created_at")
    list_filter = ("status", "reason")
    list_select_related = ("organisation", "requested_by", "resolved_by")
    search_fields = ("=sha256", "organisation__name")
    readonly_fields = (
        "organisation",
        "sha256",
        "reason",
        "overlap_share",
        "reference_ids",
        "requested_by",
        "created_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(CreatorMergeLog)
class CreatorMergeLogAdmin(admin.ModelAdmin[CreatorMergeLog]):
    """Audit trail of a staff creator merge/alias (`marceline/review/creators.py`). Read-only: the
    source row is gone by the time this exists, so nothing here is ever corrected in place."""

    list_display = (
        "source_name",
        "target",
        "as_alias",
        "moved_references",
        "moved_pages",
        "created_at",
    )
    list_filter = ("as_alias",)
    list_select_related = ("target", "merged_by")
    search_fields = ("source_name", "target__name")
    readonly_fields = (
        "source_creator_id",
        "source_name",
        "target",
        "as_alias",
        "moved_references",
        "moved_pages",
        "aliases_added",
        "merged_by",
        "created_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: CreatorMergeLog | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: CreatorMergeLog | None = None
    ) -> bool:
        return False
