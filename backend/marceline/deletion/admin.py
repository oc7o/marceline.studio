"""Marking a pilot ended, and marking one original for removal, from the admin (ADR 0045).

Both marks need an explicit confirmation tick, the way *Original ownerships* does: the mark starts
a clock that ends in an irreversible deletion, so it is never one careless save away. Marking
writes a date and deletes nothing — `customer_data_retention` does the deleting, and it is off
until `CUSTOMER_DATA_DELETION_ENABLED`.

`Data deletion records` is read-only here because it is read-only in the database too: a trigger
refuses every UPDATE and DELETE on it.

`Customer data requests` (ADR 0056, issue #133) is the fourth screen and the only one a **creator**
fills: what they asked for, when, and what the statutory deadline is. Staff move it along —
*in progress*, then *answered* — and nothing about that changes any data. Answering an export is
`manage.py export_customer_data` plus a delivery the operator makes; answering a deletion is
`manage.py end_pilot`, i.e. the screen above, which is the only place a deletion clock starts. A
request is deliberately **not** a shortcut to either: they are separate acts by a person.
"""

from typing import Any

from django import forms
from django.contrib import admin
from django.http import HttpRequest
from django.utils import timezone

from marceline.deletion.models import (
    CustomerDataRequest,
    DataDeletionRecord,
    OrganisationEnding,
    OriginalRemoval,
)
from marceline.deletion.requests import DEADLINE
from marceline.deletion.services import due_at


class ConfirmForm(forms.ModelForm[Any]):
    """A mark starts a clock that ends in a deletion; it is ticked, never merely saved."""

    confirm = forms.BooleanField(
        required=False,
        label="Yes, start the deletion clock",
        help_text="The data below is deleted once the retention window is up. Offer the export "
        "first (`manage.py export_customer_data`).",
    )

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        if not cleaned.get("confirm"):
            raise forms.ValidationError(
                "Tick “Yes, start the deletion clock” to confirm. Nothing is deleted now; the "
                "scheduled job deletes it after the retention window."
            )
        return cleaned


class OrganisationEndingForm(ConfirmForm):
    class Meta:
        model = OrganisationEnding
        fields = ("organisation", "ended_at", "reason")


class OriginalRemovalForm(ConfirmForm):
    class Meta:
        model = OriginalRemoval
        fields = ("organisation", "sha256", "removed_at", "reason", "note")


@admin.register(OrganisationEnding)
class OrganisationEndingAdmin(admin.ModelAdmin[OrganisationEnding]):
    form = OrganisationEndingForm
    list_display = ("organisation", "ended_at", "due", "exported_at", "deleted_at", "marked_by")
    list_select_related = ("organisation", "marked_by")
    search_fields = ("organisation__name",)
    readonly_fields = ("marked_by", "marked_at", "exported_at", "deleted_at")

    def get_fields(
        self, request: HttpRequest, obj: OrganisationEnding | None = None
    ) -> tuple[str, ...]:
        return ("organisation", "ended_at", "reason", "confirm", *self.readonly_fields)

    @admin.display(description="data deleted from")
    def due(self, obj: OrganisationEnding) -> str:
        return due_at(obj.ended_at).date().isoformat()

    def save_model(
        self,
        request: HttpRequest,
        obj: OrganisationEnding,
        form: forms.ModelForm[OrganisationEnding],
        change: bool,
    ) -> None:
        if request.user.is_authenticated and obj.marked_by_id is None:
            obj.marked_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(OriginalRemoval)
class OriginalRemovalAdmin(admin.ModelAdmin[OriginalRemoval]):
    form = OriginalRemovalForm
    list_display = ("sha256", "organisation", "reason", "removed_at", "due", "deleted_at")
    list_filter = ("reason",)
    list_select_related = ("organisation", "marked_by")
    search_fields = ("sha256", "organisation__name")
    readonly_fields = ("marked_by", "marked_at", "deleted_at")

    def get_fields(
        self, request: HttpRequest, obj: OriginalRemoval | None = None
    ) -> tuple[str, ...]:
        return (
            "organisation",
            "sha256",
            "removed_at",
            "reason",
            "note",
            "confirm",
            *self.readonly_fields,
        )

    @admin.display(description="data deleted from")
    def due(self, obj: OriginalRemoval) -> str:
        return due_at(obj.removed_at).date().isoformat()

    def save_model(
        self,
        request: HttpRequest,
        obj: OriginalRemoval,
        form: forms.ModelForm[OriginalRemoval],
        change: bool,
    ) -> None:
        if request.user.is_authenticated and obj.marked_by_id is None:
            obj.marked_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(DataDeletionRecord)
class DataDeletionRecordAdmin(admin.ModelAdmin[DataDeletionRecord]):
    """Read-only, because the table is append-only in the database."""

    list_display = (
        "deleted_at",
        "scope",
        "organisation_pk",
        "organisation_public_id",
        "sha256",
        "window_days",
    )
    list_filter = ("scope",)
    search_fields = ("organisation_public_id", "sha256")
    readonly_fields = tuple(
        field.name for field in DataDeletionRecord._meta.fields if field.name != "id"
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: DataDeletionRecord | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: DataDeletionRecord | None = None
    ) -> bool:
        return False


@admin.register(CustomerDataRequest)
class CustomerDataRequestAdmin(admin.ModelAdmin[CustomerDataRequest]):
    """The creator's own request (ADR 0056): visible, dated, and with the deadline computed.

    Staff change only `status` and `staff_note`. Everything else is the creator's statement of what
    they asked and when, and rewriting that would defeat the record the Art. 12(3) deadline runs
    against. There is no add button: a request is made by the creator, in the app — a staff member
    who has been asked by mail records it with `manage.py end_pilot` or by answering the mail, not
    by inventing a row here.
    """

    list_display = ("requested_at", "organisation", "kind", "status", "due", "settled_at")
    list_filter = ("kind", "status")
    list_select_related = ("organisation", "requested_by")
    search_fields = ("organisation__name", "organisation__public_id")
    readonly_fields = ("organisation", "kind", "requested_at", "requested_by", "settled_at")
    fields = (
        "organisation",
        "kind",
        "requested_at",
        "requested_by",
        "status",
        "settled_at",
        "staff_note",
    )

    @admin.display(description="answer due (Art. 12(3) GDPR)")
    def due(self, obj: CustomerDataRequest) -> str:
        return (obj.requested_at + DEADLINE).date().isoformat()

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def save_model(
        self,
        request: HttpRequest,
        obj: CustomerDataRequest,
        form: forms.ModelForm[CustomerDataRequest],
        change: bool,
    ) -> None:
        # A staff member marking it answered or withdrawn settles the clock; re-opening it clears
        # the stamp again, so the two never disagree.
        if obj.open:
            obj.settled_at = None
        elif obj.settled_at is None:
            obj.settled_at = timezone.now()
        super().save_model(request, obj, form, change)
