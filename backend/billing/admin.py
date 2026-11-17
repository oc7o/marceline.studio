"""Internal ops UI for billing: plans, customers' billing profiles, subscriptions and invoices.

The operator's workflow (ADR 0032): create a draft (or let `issue_invoices` bill subscriptions),
**Issue** it, send the PDF, and when the transfer shows up on the bank statement, **Mark paid**.
A wrong invoice is never edited or deleted once issued — **Cancel** issues a Storno, then a new
invoice is drafted. Every action goes through `billing.services`, which refuses what GoBD forbids;
the database refuses it again.
"""

from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.urls import URLPattern, path, reverse
from django.utils.html import format_html

from billing import services
from billing.models import BillingProfile, Invoice, InvoiceLine, Plan, Subscription

ISSUE_COLUMNS = (
    "number",
    "issued_on",
    "due_on",
    "vat_treatment",
    "vat_rate_bp",
    "net_cents",
    "vat_cents",
    "gross_cents",
    "seller",
    "buyer",
    "pdf_sha256",
)


def euros(cents: int | None) -> str:
    return "—" if cents is None else f"{cents / 100:,.2f} EUR"


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin[Plan]):
    list_display = ("code", "name", "interval", "price", "active")
    list_filter = ("interval", "active")
    search_fields = ("code", "name")

    @admin.display(description="Net price")
    def price(self, obj: Plan) -> str:
        return euros(obj.price_net_cents)


@admin.register(BillingProfile)
class BillingProfileAdmin(admin.ModelAdmin[BillingProfile]):
    list_display = ("legal_name", "organisation", "country", "is_business", "vat_id")
    list_filter = ("country", "is_business")
    list_select_related = ("organisation",)
    search_fields = ("legal_name", "organisation__name", "vat_id")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin[Subscription]):
    list_display = ("organisation", "plan", "starts_on", "ends_on")
    list_filter = ("plan",)
    list_select_related = ("organisation", "plan")
    search_fields = ("organisation__name",)


class InvoiceLineInline(admin.TabularInline[InvoiceLine, Invoice]):
    model = InvoiceLine
    extra = 0
    fields = ("position", "description", "quantity", "unit_net_cents")

    def _editable(self, obj: Invoice | None) -> bool:
        return obj is None or obj.number is None

    def has_add_permission(self, request: HttpRequest, obj: Invoice | None = None) -> bool:
        return self._editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request: HttpRequest, obj: Invoice | None = None) -> bool:
        return self._editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Invoice | None = None) -> bool:
        return self._editable(obj) and super().has_delete_permission(request, obj)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin[Invoice]):
    list_display = (
        "__str__",
        "organisation",
        "kind",
        "issued_on",
        "due_on",
        "gross",
        "status",
        "paid_on",
    )
    list_filter = ("kind", "vat_treatment", "issued_on")
    list_select_related = ("organisation", "cancellation")
    search_fields = ("number", "organisation__name")
    raw_id_fields = ("subscription",)
    inlines = (InvoiceLineInline,)
    actions = ("issue_drafts", "mark_paid_today", "cancel_invoices")
    readonly_fields = ("kind", "cancels", "pdf_link", *ISSUE_COLUMNS)

    def get_readonly_fields(
        self, request: HttpRequest, obj: Invoice | None = None
    ) -> tuple[str, ...]:
        if obj is not None and obj.number is not None:
            # Issued: only the payment bookkeeping stays editable.
            editable = {"paid_on", "payment_note"}
            return tuple(
                name
                for name in (
                    "organisation",
                    "subscription",
                    "period_start",
                    "period_end",
                    *self.readonly_fields,
                )
                if name not in editable
            )
        return tuple(self.readonly_fields)

    def has_delete_permission(self, request: HttpRequest, obj: Invoice | None = None) -> bool:
        if obj is not None and obj.number is not None:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request: HttpRequest, obj: Invoice, form: Any, change: bool) -> None:
        if obj.pk is not None and obj.number is not None:
            obj.save(update_fields=["paid_on", "payment_note"])
            return
        super().save_model(request, obj, form, change)

    @admin.display(description="Gross")
    def gross(self, obj: Invoice) -> str:
        return euros(obj.gross_cents)

    @admin.display(description="Status")
    def status(self, obj: Invoice) -> str:
        return services.status_of(obj, services.today())

    @admin.display(description="PDF")
    def pdf_link(self, obj: Invoice) -> str:
        if obj.pk is None or obj.number is None:
            return "—"
        url = reverse("admin:billing_invoice_pdf", args=(obj.pk,))
        return format_html('<a href="{}">{}.pdf</a>', url, obj.number)

    def get_urls(self) -> list[URLPattern]:
        return [
            path(
                "<int:invoice_id>/pdf/",
                self.admin_site.admin_view(self.pdf_view),
                name="billing_invoice_pdf",
            ),
            *super().get_urls(),
        ]

    def pdf_view(self, request: HttpRequest, invoice_id: int) -> HttpResponse:
        if not self.has_view_permission(request):
            raise Http404
        invoice = Invoice.objects.filter(pk=invoice_id, number__isnull=False).first()
        if invoice is None or invoice.pdf is None:
            raise Http404
        response = HttpResponse(bytes(invoice.pdf), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{invoice.number}.pdf"'
        return response

    def _each(
        self,
        request: HttpRequest,
        queryset: QuerySet[Invoice],
        action: Any,
        done: str,
    ) -> None:
        count = 0
        for invoice in queryset.order_by("pk"):
            try:
                action(invoice)
                count += 1
            except services.BillingError as error:
                self.message_user(request, f"{invoice}: {error}", messages.ERROR)
        if count:
            self.message_user(request, f"{done}: {count}", messages.SUCCESS)

    @admin.action(description="Issue selected drafts (assigns numbers; irreversible)")
    def issue_drafts(self, request: HttpRequest, queryset: QuerySet[Invoice]) -> None:
        self._each(request, queryset, services.issue, "Issued")

    @admin.action(description="Mark selected invoices paid today")
    def mark_paid_today(self, request: HttpRequest, queryset: QuerySet[Invoice]) -> None:
        on = services.today()
        self._each(
            request,
            queryset,
            lambda invoice: services.mark_paid(invoice, paid_on=on, note=invoice.payment_note),
            "Marked paid",
        )

    @admin.action(description="Cancel selected invoices (issues a Storno each)")
    def cancel_invoices(self, request: HttpRequest, queryset: QuerySet[Invoice]) -> None:
        self._each(request, queryset, services.cancel, "Cancelled")
