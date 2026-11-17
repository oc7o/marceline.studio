"""Internal ops UI for organisations. Customers never see this; the operator assigns here.

Accounts are created here (or with `manage.py`), never by a visitor: there is no self-service
signup anywhere in the product by decision (issue #50, ADR 0023).
"""

from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from accounts import business_status as business_status_rules
from accounts import invites as invite_service
from accounts import onboarding
from accounts.models import (
    BusinessStatus,
    CustomerInvite,
    DocumentAcceptance,
    LoginAttempt,
    Membership,
    Organisation,
)


class MembershipInline(admin.TabularInline[Membership, Organisation]):
    model = Membership
    extra = 0
    raw_id_fields = ("user",)
    readonly_fields = ("created_at",)


class BusinessStatusInline(admin.StackedInline[BusinessStatus, Organisation]):
    """The *Unternehmer* evidence, edited where the organisation is (issue #92).

    An inline rather than a screen of its own: it is filled once, at onboarding, next to the
    membership that grants access — and the operator should not have to remember a second page.
    """

    model = BusinessStatus
    extra = 0
    can_delete = False
    raw_id_fields = ("checked_by",)
    readonly_fields = ("updated_at",)
    verbose_name_plural = "business status (Unternehmer evidence, issue #92)"


class BusinessStatusEvidencedFilter(admin.SimpleListFilter):
    """ "Business status not evidenced" — the list of organisations still missing their evidence.

    The rule is Python (`accounts/business_status.py`) rather than a query, and it is kept that
    way: one definition that the model, the guard and this filter all read. Organisations are
    counted in tens, so materialising the ids costs nothing, and a second definition in SQL that
    drifts from the first would be worse than the query it saves.
    """

    title = "business status"
    parameter_name = "business_status"

    def lookups(
        self, request: HttpRequest, model_admin: admin.ModelAdmin[Organisation]
    ) -> tuple[tuple[str, str], ...]:
        return (
            ("not_evidenced", "not evidenced"),
            ("evidenced", "evidenced"),
            ("outside_eu", "outside the EU"),
        )

    def queryset(
        self, request: HttpRequest, queryset: QuerySet[Organisation]
    ) -> QuerySet[Organisation]:
        chosen = self.value()
        if chosen not in ("not_evidenced", "evidenced", "outside_eu"):
            return queryset
        organisations = queryset.select_related("business_status")
        if chosen == "outside_eu":
            matching = [
                organisation.pk
                for organisation in organisations
                if business_status_rules.OUTSIDE_EU
                in business_status_rules.business_status_codes(organisation)
            ]
        else:
            want = chosen == "evidenced"
            matching = [
                organisation.pk
                for organisation in organisations
                if organisation.business_status_evidenced is want
            ]
        return queryset.filter(pk__in=matching)


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin[Organisation]):
    list_display = ("name", "segment", "business_status_evidenced", "created_at")
    list_filter = ("segment", BusinessStatusEvidencedFilter)
    # The public id is what a customer's screens show (ADR 0033): search by it when they send one.
    search_fields = ("name", "=public_id")
    readonly_fields = ("public_id",)
    inlines = (BusinessStatusInline, MembershipInline)

    @admin.display(boolean=True, description="business evidenced")
    def business_status_evidenced(self, organisation: Organisation) -> bool:
        return organisation.business_status_evidenced

    def save_formset(self, request: HttpRequest, form: Any, formset: Any, change: bool) -> None:
        """Warn once when a membership is saved for an organisation with no evidence on file.

        `Membership.clean()` is what *refuses* (and only where the operator set the guard to
        refuse). This is the other half: in the default `warn` mode nothing stops the save, so the
        message is the whole signal, and it has to appear on the screen the row was created on.
        """
        super().save_formset(request, form, formset, change)
        if formset.model is Membership and formset.new_objects:
            warn_about_business_status(request, form.instance)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin[Membership]):
    list_display = ("user", "organisation", "role", "created_at")
    list_filter = ("role", "organisation__segment")
    list_select_related = ("user", "organisation")
    raw_id_fields = ("user",)
    search_fields = ("user__username", "organisation__name")

    def save_model(self, request: HttpRequest, obj: Membership, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        if not change:
            warn_about_business_status(request, obj.organisation)


def warn_about_business_status(request: HttpRequest, organisation: Organisation) -> None:
    """The one place the guard's message reaches a staff screen."""
    message = business_status_rules.business_status_message(organisation)
    if message is not None:
        messages.warning(request, message)


@admin.register(BusinessStatus)
class BusinessStatusAdmin(admin.ModelAdmin[BusinessStatus]):
    """Also reachable on its own, for the operator who wants the whole list in one place."""

    # `age_declared_at` is on the list on purpose: "did this account holder declare being of full
    # age, and when" is a question staff must be able to answer without opening a row (issue #123).
    list_display = (
        "organisation",
        "evidence_kind",
        "country",
        "checked_at",
        "age_declared_at",
        "updated_at",
    )
    list_filter = ("evidence_kind",)
    search_fields = ("organisation__name", "=vat_id")
    raw_id_fields = ("organisation", "checked_by")
    readonly_fields = ("updated_at",)


@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin[LoginAttempt]):
    """Read-only: how many failed logins are currently counted, and against which digest.

    The keys are HMACs, so this screen can say "an account is locked" but never which one. Lifting
    a lockout is `manage.py unlock_login --user NAME`, which knows the username and can compute the
    digest; deleting rows here by hand would mean guessing.
    """

    list_display = ("scope", "key", "created_at")
    list_filter = ("scope",)
    readonly_fields = ("scope", "key", "created_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(CustomerInvite)
class CustomerInviteAdmin(admin.ModelAdmin[CustomerInvite]):
    """Issue, watch and revoke an invite (issue #115, ADR 0052).

    **The link is shown once, here, when the row is added** — the token is never stored, so this
    screen is the only moment it exists. Copy it into the message being written to the creator;
    the project sends no customer mail (ADR 0052 §2), and there is no way to get the link back.

    Adding a row *is* the "issue an invite" action: a Django admin action cannot ask for the
    address, and a form that does is both simpler and the place the expiry and the note belong.
    """

    list_display = ("id", "destination", "state", "created_at", "expires_at", "outside_eu_answers")
    list_filter = ("segment",)
    list_select_related = ("organisation",)
    raw_id_fields = ("organisation", "invited_by", "redeemed_user")
    # Everything the redemption writes is the customer's own answer, and the digest is not editable
    # by anyone: staff issue and revoke, they do not edit a half-finished redemption.
    readonly_fields = (
        "token_hash",
        "created_at",
        "redeemed_at",
        "redeemed_user",
        "password_hash",
        "declaration_version",
        "declaration_text",
        "declared_at",
        "age_declaration_version",
        "age_declaration_text",
        "age_declared_at",
        "country",
        "outside_eu_answers",
        "revoked_at",
    )
    actions = ("revoke_invites",)

    @admin.display(description="state")
    def state(self, invite: CustomerInvite) -> str:
        """What staff need to tell apart. The *customer* is never told which of these applies."""
        if invite.redeemed_at is not None:
            return "redeemed"
        if invite.revoked_at is not None:
            return "revoked"
        if not invite_service.usable(invite):
            return "expired"
        return f"open ({invite_service.step_of(invite).value})"

    def get_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        if obj is None:
            return ("organisation", "organisation_name", "segment", "email", "note", "expires_at")
        return (
            "organisation",
            "organisation_name",
            "segment",
            "email",
            "note",
            "expires_at",
            "token_hash",
            "created_at",
            "redeemed_at",
            "redeemed_user",
            "declaration_version",
            "declaration_text",
            "declared_at",
            "age_declaration_version",
            "age_declaration_text",
            "age_declared_at",
            "country",
            "outside_eu_answers",
            "revoked_at",
            "revoked_reason",
        )

    def save_model(
        self, request: HttpRequest, obj: CustomerInvite, form: Any, change: bool
    ) -> None:
        """On add: mint the token, store only its digest, and show the link exactly once."""
        if change:
            super().save_model(request, obj, form, change)
            return
        if not onboarding.invites_enabled():
            messages.error(
                request,
                "ONBOARDING_INVITES_ENABLED is off (issue #115, ADR 0052): the invite is saved "
                "but its link answers the same neutral 404 as an unknown token until it is on.",
            )
        token = onboarding.new_token()
        obj.token_hash = onboarding.token_hash(token)
        obj.invited_by = request.user if request.user.is_authenticated else None
        super().save_model(request, obj, form, change)
        messages.warning(
            request,
            "Copy this link now — it is shown once and cannot be recovered: "
            + invite_service.invite_link(token),
        )
        if not onboarding.documents_available():
            messages.warning(
                request,
                "ONBOARDING_DOCUMENTS names no published pilot agreement and privacy notice, so "
                "this invite cannot be completed yet (ADR 0052 §3).",
            )

    @admin.action(description="Revoke the selected invites")
    def revoke_invites(self, request: HttpRequest, queryset: QuerySet[CustomerInvite]) -> None:
        revoked = 0
        for invite in queryset:
            if invite.revoked_at is None and invite.redeemed_at is None:
                invite_service.revoke(invite, reason=f"revoked in the admin by {request.user}")
                revoked += 1
        messages.info(request, f"{revoked} invite(s) revoked; their links answer the neutral page.")


@admin.register(DocumentAcceptance)
class DocumentAcceptanceAdmin(admin.ModelAdmin[DocumentAcceptance]):
    """Read-only: what was accepted, in which version, at which moment (ADR 0052 §3).

    Append-only by decision, so the admin offers no add, change or delete. A wrong row is a bug in
    the flow, not something to correct by hand — correcting it would destroy the only record of
    what somebody actually agreed to.
    """

    list_display = ("id", "document_id", "version", "accepted_at", "user", "organisation")
    list_filter = ("document_id", "version")
    list_select_related = ("user", "organisation")
    raw_id_fields = ("invite", "user", "organisation")
    readonly_fields = tuple(field.name for field in DocumentAcceptance._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
