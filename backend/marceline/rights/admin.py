"""Staff side of the rights model (ADR 0034): record, dispute, revoke, each with a reason.

Every status change goes through `marceline.rights.services`, never through a form field: status
fields are read-only here and changes are admin actions that show the selected rows and ask for a
reason before anything happens. Logs are read-only, nothing is deletable (retention deletes), and
proof documents are downloaded only through `download_view`, which logs the access first.

**What staff are asked here is a formal-completeness check, never what a document means**
(`docs/legal/answers/03-rdg-operating-model.md` Q3, ADR 0034 amendment 2026-09-18): whether the
parties, the work, the dates and a signature are visibly present, and whether the dates still run.
Reading a production contract, a licence or a mandate and concluding that it legally supports the
claimed right or authority is an examination of the customer's concrete matter — nobody here does
that, and no wording on these screens asks for it. When the answer would depend on what a clause
means, stop and leave the record where it is.
"""

from collections.abc import Callable
from typing import Any, cast

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.urls import URLPattern, path, reverse
from django.utils.html import format_html

from accounts.models import Organisation
from marceline.rights import account_control, services, vault
from marceline.rights import account_control_services as verification
from marceline.rights.models import (
    CHALLENGE_METHODS,
    AccountControlChallenge,
    AccountControlEvent,
    AgencyMandate,
    IdentityMethod,
    LicensedPartner,
    ProofDocument,
    RightsClaimant,
    RightsEvent,
    RightsSchedule,
    RightsStatus,
    VerificationMethod,
    WorkRights,
    WorkRightsTransition,
)

S = RightsStatus


class ReasonForm(forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "cols": 60}),
        help_text="Stored in the log with your account. Note what you saw — which document, which "
        "fields were present, which dates. Not what you concluded it means.",
    )


class IdentityForm(forms.Form):
    method = forms.ChoiceField(choices=IdentityMethod.choices)
    evidence_note = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "cols": 60}),
        help_text="E.g. register court and number, the company domain, where the token was seen. "
        "Never copy ID document data here.",
    )


class CompetingClaimForm(ReasonForm):
    organisation = forms.ModelChoiceField(queryset=Organisation.objects.order_by("name"))


def staff_actor(request: HttpRequest) -> services.Actor:
    return services.Actor.staff(cast(User, request.user))


def confirmed_action(
    modeladmin: admin.ModelAdmin[Any],
    request: HttpRequest,
    queryset: QuerySet[Any],
    *,
    action: str,
    title: str,
    explanation: str,
    form_class: type[forms.Form],
    apply: Callable[[Any, dict[str, Any]], object],
) -> TemplateResponse | None:
    """Show the selected rows and a form; on submit, apply per row and report refusals."""
    form = form_class(request.POST) if "apply" in request.POST else form_class()
    if "apply" in request.POST and form.is_valid():
        done = 0
        for obj in queryset.order_by("pk"):
            try:
                apply(obj, form.cleaned_data)
                done += 1
            except services.RightsError as error:
                modeladmin.message_user(request, f"{obj}: {error}", messages.ERROR)
        if done:
            modeladmin.message_user(request, f"{title}: {done}", messages.SUCCESS)
        return None
    return TemplateResponse(
        request,
        "admin/marceline/rights_confirm.html",
        {
            **modeladmin.admin_site.each_context(request),
            "title": title,
            "explanation": explanation,
            "objects": queryset.order_by("pk"),
            "form": form,
            "action": action,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "opts": modeladmin.model._meta,
        },
    )


class ReadOnlyAdmin(admin.ModelAdmin[Any]):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class NoDeleteAdmin(admin.ModelAdmin[Any]):
    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(RightsClaimant)
class RightsClaimantAdmin(NoDeleteAdmin):
    list_display = (
        "organisation",
        "claimant_type",
        "identity_status",
        "identity_method",
        "identity_verified_at",
        "partners_none_affirmed_at",
    )
    list_filter = ("claimant_type", "identity_status")
    list_select_related = ("organisation",)
    search_fields = ("organisation__name",)
    actions = ("verify_identity", "revoke_identity", "issue_token")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        fields = tuple(field.name for field in RightsClaimant._meta.fields)
        return fields if obj is not None else tuple(f for f in fields if f != "organisation")

    def get_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        if obj is None:
            return ("organisation",)
        return tuple(field.name for field in RightsClaimant._meta.fields)

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        services.claimant_for(obj.organisation)

    @admin.action(description="Verify identity… (method and evidence note)")
    def verify_identity(
        self, request: HttpRequest, queryset: QuerySet[RightsClaimant]
    ) -> TemplateResponse | None:
        return confirmed_action(
            self,
            request,
            queryset,
            action="verify_identity",
            title="Verify identity",
            explanation="Records who stands behind these organisations and how you checked it. "
            "This verifies identity only, never a right to a work.",
            form_class=IdentityForm,
            apply=lambda claimant, data: services.verify_identity(
                claimant.organisation,
                method=data["method"],
                evidence_note=data["evidence_note"],
                actor=staff_actor(request),
            ),
        )

    @admin.action(description="Revoke identity verification…")
    def revoke_identity(
        self, request: HttpRequest, queryset: QuerySet[RightsClaimant]
    ) -> TemplateResponse | None:
        return confirmed_action(
            self,
            request,
            queryset,
            action="revoke_identity",
            title="Revoke identity verification",
            explanation="Blocks notice drafts for every work of these organisations.",
            form_class=ReasonForm,
            apply=lambda claimant, data: services.revoke_identity(
                claimant.organisation, reason=data["reason"], actor=staff_actor(request)
            ),
        )

    @admin.action(description="Issue a new account-control token")
    def issue_token(self, request: HttpRequest, queryset: QuerySet[RightsClaimant]) -> None:
        for claimant in queryset:
            services.issue_account_control_token(claimant.organisation, staff_actor(request))
        self.message_user(request, "Tokens issued; the customer sees theirs on the rights screen.")


class TransitionInline(admin.TabularInline[WorkRightsTransition, WorkRights]):
    model = WorkRightsTransition
    fields = ("at", "from_status", "to_status", "actor_kind", "actor_label", "code", "reason")
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class WorkRightsForm(forms.ModelForm[WorkRights]):
    class Meta:
        model = WorkRights
        fields = ("schedule", "mandate", "legal_hold")

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        for field in ("schedule", "mandate"):
            linked = cleaned.get(field)
            if linked is not None and linked.organisation_id != self.instance.organisation_id:
                self.add_error(field, "Belongs to another organisation.")
        return cleaned


def _transition_action(to_status: str, code: str, title: str, explanation: str) -> Any:
    def action(
        modeladmin: "WorkRightsAdmin", request: HttpRequest, queryset: QuerySet[WorkRights]
    ) -> TemplateResponse | None:
        return confirmed_action(
            modeladmin,
            request,
            queryset,
            action=f"to_{to_status}",
            title=title,
            explanation=explanation,
            form_class=ReasonForm,
            apply=lambda work, data: services.transition(
                work, to_status, staff_actor(request), reason=data["reason"], code=code
            ),
        )

    action.__name__ = f"to_{to_status}"
    return admin.action(description=f"{title}…")(action)


@admin.register(WorkRights)
class WorkRightsAdmin(NoDeleteAdmin):
    form = WorkRightsForm
    list_display = (
        "pk",
        "organisation",
        "short_sha256",
        "status",
        "capacity",
        "asserted_right",
        "schedule",
        "mandate",
        "status_changed_at",
        "legal_hold",
    )
    list_filter = ("status", "capacity", "asserted_right", "legal_hold", "organisation")
    list_select_related = ("organisation", "schedule", "mandate")
    search_fields = ("sha256", "organisation__name", "underlying_owner_name")
    raw_id_fields = ("schedule", "mandate")
    inlines = (TransitionInline,)
    actions = (
        _transition_action(
            S.DOCUMENTS_RECEIVED,
            "documents_received",
            "Mark documents received",
            "You have the rights-relevant documents for these works (not yet verified).",
        ),
        _transition_action(
            S.DOCUMENTS_COMPLETE,
            "formal_check",
            "Record the formal document check",
            "A formal-completeness check, not an assessment of what the documents mean: the "
            "parties are named, the work or catalogue is identified, the document carries a date "
            "and a signature, and the period it names still runs. Do not decide whether the "
            "wording legally supports the asserted right — if that is the open question, leave "
            "the record in documents_received and say so in the reason. Only records in "
            "documents_received can be checked, and the record goes no further here: whether the "
            "documents give the claimed right or authority is the customer's own declaration, "
            "which only they can make, on their rights screen.",
        ),
        _transition_action(
            S.DISPUTED,
            "disputed",
            "Flag as disputed",
            "Blocks notice drafts until the dispute is resolved.",
        ),
        _transition_action(
            S.REVOKED, "revoked", "Revoke", "The right or authority no longer stands."
        ),
        _transition_action(
            S.UNVERIFIED,
            "reopened",
            "Reopen as unverified",
            "For disputed, revoked or expired records that should be checked again from scratch.",
        ),
        "supersede",
        "record_competing_claim",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        editable = {"schedule", "mandate", "legal_hold"}
        return tuple(f.name for f in WorkRights._meta.fields if f.name not in editable)

    @admin.display(description="sha256")
    def short_sha256(self, obj: WorkRights) -> str:
        return f"{obj.sha256[:12]}…"

    @admin.action(description="Supersede (close and open a fresh unverified record)…")
    def supersede(
        self, request: HttpRequest, queryset: QuerySet[WorkRights]
    ) -> TemplateResponse | None:
        return confirmed_action(
            self,
            request,
            queryset,
            action="supersede",
            title="Supersede",
            explanation="Closes these records for good and opens a new unverified record per work.",
            form_class=ReasonForm,
            apply=lambda work, data: services.supersede(
                work, reason=data["reason"], actor=staff_actor(request)
            ),
        )

    @admin.action(description="Record a competing claim by another organisation…")
    def record_competing_claim(
        self, request: HttpRequest, queryset: QuerySet[WorkRights]
    ) -> TemplateResponse | None:
        return confirmed_action(
            self,
            request,
            queryset,
            action="record_competing_claim",
            title="Record competing claim",
            explanation="Opens a record for the claiming organisation and disputes every current "
            "record of these works.",
            form_class=CompetingClaimForm,
            apply=lambda work, data: services.record_competing_claim(
                data["organisation"], work.sha256, reason=data["reason"], actor=staff_actor(request)
            ),
        )


@admin.register(AgencyMandate)
class AgencyMandateAdmin(NoDeleteAdmin):
    list_display = (
        "pk",
        "organisation",
        "principal_name",
        "agent_name",
        "authority_to_prepare_notices",
        "valid_from",
        "valid_until",
        "revoked_at",
    )
    list_filter = ("authority_to_prepare_notices", "whole_catalogue", "organisation")
    list_select_related = ("organisation",)
    search_fields = ("principal_name", "agent_name", "organisation__name")
    readonly_fields = ("revoked_at", "revoked_reason", "created_at")
    actions = ("revoke",)

    @admin.action(description="Revoke mandate (its works become revoked)…")
    def revoke(
        self, request: HttpRequest, queryset: QuerySet[AgencyMandate]
    ) -> TemplateResponse | None:
        return confirmed_action(
            self,
            request,
            queryset,
            action="revoke",
            title="Revoke mandate",
            explanation="Every work acting under these mandates moves to revoked.",
            form_class=ReasonForm,
            apply=lambda mandate, data: services.revoke_mandate(
                mandate, reason=data["reason"], actor=staff_actor(request)
            ),
        )


@admin.register(RightsSchedule)
class RightsScheduleAdmin(NoDeleteAdmin):
    list_display = ("pk", "name", "organisation", "valid_until", "legal_hold", "created_at")
    list_select_related = ("organisation",)
    search_fields = ("name", "organisation__name")
    actions = ("verify_works",)

    @admin.action(description="Record the formal check for every work on the schedule…")
    def verify_works(
        self, request: HttpRequest, queryset: QuerySet[RightsSchedule]
    ) -> TemplateResponse | None:
        return confirmed_action(
            self,
            request,
            queryset,
            action="verify_works",
            title="Record the schedule's formal check",
            explanation="One document, many works: each linked work is marked documents received "
            "and formally checked, one logged transition per work. The same formal-completeness "
            "check as for a single work — the schedule lists these works, it is dated and signed, "
            "and its period still runs. Whether its wording legally covers them is not decided "
            "here.",
            form_class=ReasonForm,
            apply=lambda schedule, data: services.verify_schedule(
                schedule, reason=data["reason"], actor=staff_actor(request)
            ),
        )


@admin.register(LicensedPartner)
class LicensedPartnerAdmin(ReadOnlyAdmin):
    list_display = ("name", "kind", "domain", "path_prefix", "organisation", "created_at")
    list_filter = ("kind",)
    list_select_related = ("organisation",)
    search_fields = ("name", "domain", "organisation__name")


class ProofDocumentForm(forms.ModelForm[ProofDocument]):
    class Meta:
        model = ProofDocument
        fields = ("work", "schedule", "mandate", "legal_hold")

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        for field in ("work", "schedule", "mandate"):
            linked = cleaned.get(field)
            if linked is not None and linked.organisation_id != self.instance.organisation_id:
                self.add_error(field, "Belongs to another organisation.")
        return cleaned


@admin.register(ProofDocument)
class ProofDocumentAdmin(NoDeleteAdmin):
    form = ProofDocumentForm
    list_display = (
        "pk",
        "organisation",
        "kind",
        "original_filename",
        "size_bytes",
        "uploaded_via",
        "uploaded_at",
        "download_link",
    )
    list_filter = ("kind", "uploaded_via")
    list_select_related = ("organisation",)
    search_fields = ("original_filename", "organisation__name")
    raw_id_fields = ("work", "schedule", "mandate")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        editable = {"work", "schedule", "mandate", "legal_hold"}
        fields = tuple(f.name for f in ProofDocument._meta.fields if f.name not in editable)
        return (*fields, "download_link")

    @admin.display(description="Document")
    def download_link(self, obj: ProofDocument) -> str:
        if obj.pk is None:
            return "—"
        url = reverse("admin:marceline_proofdocument_download", args=(obj.pk,))
        return format_html('<a href="{}">download (logged)</a>', url)

    def get_urls(self) -> list[URLPattern]:
        return [
            path(
                "<int:proof_id>/download/",
                self.admin_site.admin_view(self.download_view),
                name="marceline_proofdocument_download",
            ),
            *super().get_urls(),
        ]

    def download_view(self, request: HttpRequest, proof_id: int) -> HttpResponse:
        """Staff only (admin_view), logged before decrypting; never inline, never cached."""
        if not self.has_view_permission(request):
            raise Http404
        proof = ProofDocument.objects.select_related("organisation").filter(pk=proof_id).first()
        if proof is None:
            raise Http404
        try:
            data = services.read_proof(proof, staff_actor(request))
        except vault.VaultError as error:
            return HttpResponse(
                f"The document cannot be read: {error.code}", status=503, content_type="text/plain"
            )
        suffix = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}
        response = HttpResponse(data, content_type=proof.content_type)
        response["Content-Disposition"] = (
            f'attachment; filename="proof-{proof.pk}.{suffix.get(proof.content_type, "bin")}"'
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "sandbox"
        return response


@admin.register(WorkRightsTransition)
class WorkRightsTransitionAdmin(ReadOnlyAdmin):
    list_display = ("at", "work", "from_status", "to_status", "actor_kind", "actor_label", "code")
    list_filter = ("to_status", "actor_kind", "code")
    list_select_related = ("work",)
    raw_id_fields = ("work",)


@admin.register(RightsEvent)
class RightsEventAdmin(ReadOnlyAdmin):
    list_display = ("at", "organisation", "kind", "proof", "actor_kind", "actor_label")
    list_filter = ("kind", "actor_kind")
    list_select_related = ("organisation", "proof")
    raw_id_fields = ("proof", "mandate", "schedule")


# --- proof of control of a creator's platform account (ADR 0055) ---------------------------------
#
# The check itself is not in this file and cannot be: a staff member opens the **public** profile in
# their own browser, reads the code with their own eyes and records what they saw. Nothing here
# fetches the claimed address — it is printed as copyable text, and `account_control.py` says why.
#
# What must never appear on these screens: a field for an identity document, a document number or a
# date of birth. The `video_call` fallback records that a check happened (date, staff user, method)
# and nothing about what was shown (operator decision 2026-09-19).

#: What the form says next to the note, every time it is offered.
NOTE_HELP = (
    "Optional. What you saw and where: the platform, that the code matched, the date. "
    "NEVER a document number, NEVER a date of birth, never an image — we do not keep those."
)


class VerifyAccountForm(forms.Form):
    method = forms.ChoiceField(
        choices=[
            (method, VerificationMethod(method).label)
            for method in VerificationMethod.values
            if method in CHALLENGE_METHODS
        ],
        help_text="How you established it. `bio_code`/`dm_code` need the code still to be live; "
        "a video call does not, and records only that the check happened.",
    )
    note = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3, "cols": 60}), help_text=NOTE_HELP
    )


class NoteForm(forms.Form):
    note = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 3, "cols": 60}), help_text=NOTE_HELP
    )


class ClaimAccountForm(forms.ModelForm[AccountControlChallenge]):
    """Issuing a challenge by hand: organisation, platform, handle, and optionally the address.

    The refusals a staff member can actually cause are answered here rather than in `save_model`,
    where a raised error would be a 500: the feature being switched off, and a handle or address we
    will not store.
    """

    class Meta:
        model = AccountControlChallenge
        fields = ("organisation", "platform", "handle", "profile_url")

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        if not verification.available():
            raise forms.ValidationError(
                "ACCOUNT_CONTROL_VERIFICATION_ENABLED is off in this container: no code can be "
                "issued. Set it, recreate `web`, and try again."
            )
        try:
            cleaned["handle"] = account_control.normalise_handle(cleaned.get("handle", ""))
        except account_control.InvalidClaimError as error:
            self.add_error("handle", error.code)
        try:
            cleaned["profile_url"] = account_control.checked_profile_url(
                cleaned.get("platform", ""), cleaned.get("profile_url", "")
            )
        except account_control.InvalidClaimError as error:
            self.add_error("profile_url", error.code)
        return cleaned


class AccountControlEventInline(admin.TabularInline[AccountControlEvent, AccountControlChallenge]):
    model = AccountControlEvent
    fields = ("at", "kind", "method", "actor_kind", "actor_label", "note")
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(AccountControlChallenge)
class AccountControlChallengeAdmin(NoDeleteAdmin):
    list_display = (
        "organisation",
        "platform",
        "handle",
        "state",
        "code",
        "code_expires_at",
        "issue_count",
        "method",
        "decided_at",
    )
    list_filter = ("state", "platform", "method")
    list_select_related = ("organisation",)
    search_fields = ("organisation__name", "handle")
    actions = ("reissue", "verify_account", "reject_account", "revoke_account")
    inlines = (AccountControlEventInline,)

    def get_form(
        self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any
    ) -> type[forms.ModelForm[Any]]:
        if obj is None:
            kwargs["form"] = ClaimAccountForm
        return super().get_form(request, obj, change, **kwargs)

    def get_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        if obj is None:
            return ("organisation", "platform", "handle", "profile_url")
        return (
            "organisation",
            "platform",
            "handle",
            "claimed_profile",
            "state",
            "code",
            "code_issued_at",
            "code_expires_at",
            "issue_count",
            "placed_at",
            "placed_by",
            "method",
            "decided_at",
            "decided_by",
            "note",
            "created_at",
        )

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        if obj is None:
            return ()
        return self.get_fields(request, obj)

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        """Issue it through the service, which is the only writer here (never `obj.save()`)."""
        challenge = verification.claim_account(
            obj.organisation,
            verification.ClaimInput(
                platform=obj.platform, handle=obj.handle, profile_url=obj.profile_url
            ),
            staff_actor(request),
        )
        obj.pk = challenge.pk
        obj.refresh_from_db()

    @admin.display(description="claimed profile (open it yourself)")
    def claimed_profile(self, obj: AccountControlChallenge) -> str:
        """The claim as copyable text. **Never a link**: the address is written by the creator, and
        a clickable one in a staff screen is one mis-click away from us fetching it."""
        return f"{obj.platform} / {obj.handle}" + (
            f"  —  {obj.profile_url}" if obj.profile_url else "  —  (no address given)"
        )

    @admin.action(description="Issue a new code (the previous one stops working)")
    def reissue(self, request: HttpRequest, queryset: QuerySet[AccountControlChallenge]) -> None:
        for challenge in queryset.order_by("pk"):
            try:
                verification.reissue_code(challenge, staff_actor(request))
            except services.RightsError as error:
                self.message_user(request, f"{challenge}: {error}", messages.ERROR)

    @admin.action(description="Record that you saw it… (method and optional note)")
    def verify_account(
        self, request: HttpRequest, queryset: QuerySet[AccountControlChallenge]
    ) -> TemplateResponse | None:
        return confirmed_action(
            self,
            request,
            queryset,
            action="verify_account",
            title="Record account control",
            explanation="You opened the public profile yourself and the code matched (or you held "
            "the video call). This records identity for the whole organisation and is what lets a "
            "notice draft be prepared. It says nothing about rights in any work. A claim on the "
            "creator's own website or on a platform not in our list takes `video_call` only: "
            "nothing checked who they are before they published there, so a code proves nothing "
            "about identity (the database refuses the other two methods).",
            form_class=VerifyAccountForm,
            apply=lambda challenge, data: verification.verify(
                challenge,
                method=data["method"],
                note=data.get("note", ""),
                actor=staff_actor(request),
            ),
        )

    @admin.action(description="Record that you did not see it…")
    def reject_account(
        self, request: HttpRequest, queryset: QuerySet[AccountControlChallenge]
    ) -> TemplateResponse | None:
        return confirmed_action(
            self,
            request,
            queryset,
            action="reject_account",
            title="Record that the code was not there",
            explanation="Identity is left exactly as it was. The creator can claim the account "
            "again and gets a new code.",
            form_class=NoteForm,
            apply=lambda challenge, data: verification.reject(
                challenge, note=data.get("note", ""), actor=staff_actor(request)
            ),
        )

    @admin.action(description="Take a verification back…")
    def revoke_account(
        self, request: HttpRequest, queryset: QuerySet[AccountControlChallenge]
    ) -> TemplateResponse | None:
        return confirmed_action(
            self,
            request,
            queryset,
            action="revoke_account",
            title="Revoke account control",
            explanation="With no other verified account left, the organisation's identity check is "
            "revoked too, which blocks notice drafts for every one of its works.",
            form_class=ReasonForm,
            apply=lambda challenge, data: verification.revoke_verification(
                challenge, reason=data["reason"], actor=staff_actor(request)
            ),
        )


@admin.register(AccountControlEvent)
class AccountControlEventAdmin(ReadOnlyAdmin):
    list_display = ("at", "challenge", "kind", "method", "actor_kind", "actor_label")
    list_filter = ("kind", "method", "actor_kind")
    list_select_related = ("challenge",)
    raw_id_fields = ("challenge",)
