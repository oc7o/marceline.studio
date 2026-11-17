"""Plans, subscriptions and invoices: provider-agnostic billing (ADR 0032, issue #35).

Billing starts with **invoices and SEPA bank transfer** (operator decision, 2026-09-17: no card
processor). Nothing here names a payment provider. A card or direct-debit processor, if one is
ever added, is a thin adapter that records a payment against an `Invoice` — the plan, the
subscription and the invoice stay ours.

Money is **integer euro cents** throughout (`*_cents`), VAT rates are **basis points** (1900 =
19 %). The currency is always EUR.

German bookkeeping rules (GoBD, § 14 UStG) shape the invoice table, and they are enforced by the
database, not only by the code that writes it (migration `0002_immutable_invoices`):

- an invoice gets its **number when it is issued**, from `InvoiceSequence`: sequential, gap-free,
  never reused. A draft has no number and may be edited or deleted;
- an **issued invoice is immutable** — a trigger refuses every UPDATE except the payment
  bookkeeping columns (`paid_on`, `payment_note`), and its line items can no longer be inserted,
  changed or deleted. It stays that way **until it is deleted**: past its statutory retention
  (eight years, counted from the end of the issuing year — ADR 0032 amendment 2026-09-17,
  `billing/retention.py`) the same trigger (amended by migration `0004`) allows a DELETE it
  refused before that date;
- an issued invoice is **cancelled by a second document** (`kind=cancellation`, a Storno with
  negated amounts) that points at it through `cancels`. The original row is never touched.

Seller and buyer identity are **snapshots** (`seller`, `buyer`) taken when the invoice is issued,
and the PDF is rendered once, then and there, and stored (`pdf`, `pdf_sha256`): a later change of
address, bank account or code must not change a document that was already sent.
"""

from django.db import models
from django.db.models import F, Q

from accounts.models import Organisation
from core.public_ids import public_id_field

CURRENCY = "EUR"


class Plan(models.Model):
    """What a customer can subscribe to. Prices are net (before VAT), in cents, per interval."""

    class Interval(models.TextChoices):
        MONTH = "month"
        YEAR = "year"

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=200, help_text="Printed on the invoice line.")
    interval = models.CharField(max_length=8, choices=Interval)
    price_net_cents = models.PositiveIntegerField(help_text="Net price per interval, euro cents.")
    active = models.BooleanField(default=True, help_text="Inactive plans take no new subscribers.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.interval}, {self.price_net_cents / 100:.2f} {CURRENCY} net)"


class BillingProfile(models.Model):
    """Who an organisation's invoices are addressed to. Copied onto each invoice when it is issued.

    `country` and `vat_id` decide the VAT treatment (`billing/vat.py`): German customers pay 19 %,
    EU businesses with a VAT ID get a reverse-charge invoice, and everything else is refused until
    it is deliberately supported (consumers outside Germany would need OSS).
    """

    organisation = models.OneToOneField(
        Organisation, on_delete=models.PROTECT, related_name="billing_profile"
    )
    legal_name = models.CharField(max_length=200)
    address = models.TextField(help_text="Postal address, one line per line; without the country.")
    country = models.CharField(max_length=2, help_text="ISO 3166-1 alpha-2, e.g. DE.")
    is_business = models.BooleanField(
        default=True, help_text="A business (B2B) rather than a consumer."
    )
    vat_id = models.CharField(
        max_length=20, blank=True, help_text="USt-IdNr. / VAT ID, e.g. ATU12345678."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=Q(country__regex=r"^[A-Z]{2}$"), name="billing_profile_country_iso"
            ),
        )

    def __str__(self) -> str:
        return f"{self.legal_name} ({self.country})"


class Subscription(models.Model):
    """An organisation on a plan, billed in advance per period anchored at `starts_on`.

    A period starts on `starts_on` plus a whole number of intervals (`billing/periods.py`), so a
    subscription started on 31 January is billed on 28/29 February, 31 March, 30 April, … . No
    proration: `ends_on` only stops *new* periods; a period that starts before it is billed in
    full.
    """

    organisation = models.ForeignKey(
        Organisation, on_delete=models.PROTECT, related_name="subscriptions"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    starts_on = models.DateField()
    ends_on = models.DateField(
        null=True, blank=True, help_text="No period starting on or after this day is billed."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("organisation",),
                condition=Q(ends_on__isnull=True),
                name="subscription_one_open_per_organisation",
            ),
            models.CheckConstraint(
                condition=Q(ends_on__isnull=True) | Q(ends_on__gt=F("starts_on")),
                name="subscription_ends_after_start",
            ),
        )

    def __str__(self) -> str:
        return f"{self.organisation_id} on {self.plan_id} from {self.starts_on}"


class InvoiceSequence(models.Model):
    """The last invoice number handed out per calendar year. Only `billing/numbering.py` writes it.

    The row is locked for the issuing transaction, so a rolled-back issue gives its number back and
    two concurrent issues cannot take the same one: numbers are gap-free and never reused. A trigger
    refuses to lower `last_number` or delete a row.
    """

    year = models.PositiveIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.year}: {self.last_number}"


class Invoice(models.Model):
    """An invoice or a cancellation (Storno) of one. Draft until issued; immutable afterwards."""

    class Kind(models.TextChoices):
        INVOICE = "invoice"
        CANCELLATION = "cancellation"

    class VatTreatment(models.TextChoices):
        # German customer: 19 % German VAT.
        DOMESTIC = "domestic"
        # EU business customer with a VAT ID: no German VAT, the customer accounts for it.
        REVERSE_CHARGE = "reverse_charge"
        # The seller uses the small-business scheme (§ 19 UStG): no VAT charged.
        SMALL_BUSINESS = "small_business"

    organisation = models.ForeignKey(
        Organisation, on_delete=models.PROTECT, related_name="invoices"
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoices",
        help_text="Set for invoices issued by `issue_invoices`; empty for manual invoices.",
    )
    kind = models.CharField(max_length=16, choices=Kind, default=Kind.INVOICE)
    cancels = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cancellation",
        help_text="The invoice this cancellation document cancels.",
    )
    period_start = models.DateField(null=True, blank=True, help_text="Service period (first day).")
    period_end = models.DateField(null=True, blank=True, help_text="Service period (last day).")

    # Written once, when the invoice is issued (`billing.services.issue`); NULL while a draft.
    number = models.CharField(max_length=40, null=True, blank=True, unique=True)
    issued_on = models.DateField(null=True, blank=True)
    due_on = models.DateField(null=True, blank=True)
    vat_treatment = models.CharField(max_length=16, choices=VatTreatment, blank=True)
    vat_rate_bp = models.PositiveIntegerField(
        null=True, blank=True, help_text="VAT rate in basis points (1900 = 19 %)."
    )
    net_cents = models.IntegerField(null=True, blank=True)
    vat_cents = models.IntegerField(null=True, blank=True)
    gross_cents = models.IntegerField(null=True, blank=True)
    seller = models.JSONField(null=True, blank=True, help_text="Seller identity when issued.")
    buyer = models.JSONField(null=True, blank=True, help_text="Buyer identity when issued.")
    pdf = models.BinaryField(null=True, blank=True, editable=False)
    pdf_sha256 = models.CharField(max_length=64, blank=True)

    # Payment bookkeeping: the only columns an issued invoice lets change (bank reconciliation by
    # hand, in the admin).
    paid_on = models.DateField(null=True, blank=True)
    payment_note = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    #: What the customer API calls this document (`inv_…`, ADR 0033); the primary key never leaves.
    #: Set on create, so it is frozen with every other column once the invoice is issued.
    public_id = public_id_field("inv")

    class Meta:
        constraints = (
            # Issued means every issue-time column is present; a draft has none of them.
            models.CheckConstraint(
                condition=Q(
                    number__isnull=True,
                    issued_on__isnull=True,
                    net_cents__isnull=True,
                    vat_cents__isnull=True,
                    gross_cents__isnull=True,
                    vat_rate_bp__isnull=True,
                    seller__isnull=True,
                    buyer__isnull=True,
                    pdf__isnull=True,
                    vat_treatment="",
                    pdf_sha256="",
                )
                | Q(
                    number__isnull=False,
                    issued_on__isnull=False,
                    net_cents__isnull=False,
                    vat_cents__isnull=False,
                    gross_cents__isnull=False,
                    vat_rate_bp__isnull=False,
                    seller__isnull=False,
                    buyer__isnull=False,
                    pdf__isnull=False,
                )
                & ~Q(vat_treatment="")
                & ~Q(pdf_sha256=""),
                name="invoice_issued_complete",
            ),
            models.CheckConstraint(
                condition=Q(gross_cents__isnull=True)
                | Q(gross_cents=F("net_cents") + F("vat_cents")),
                name="invoice_gross_is_net_plus_vat",
            ),
            models.CheckConstraint(
                condition=Q(kind="invoice", cancels__isnull=True)
                | Q(kind="cancellation", cancels__isnull=False),
                name="invoice_cancellation_names_its_invoice",
            ),
            models.CheckConstraint(
                condition=Q(net_cents__isnull=True)
                | Q(kind="invoice", net_cents__gte=0)
                | Q(kind="cancellation", net_cents__lte=0),
                name="invoice_amount_sign_matches_kind",
            ),
            # Only an issued invoice (not a cancellation) can be paid, and only an issued invoice
            # has a due date.
            models.CheckConstraint(
                condition=Q(paid_on__isnull=True) | Q(kind="invoice", number__isnull=False),
                name="invoice_paid_only_when_issued",
            ),
            models.CheckConstraint(
                condition=Q(period_start__isnull=True, period_end__isnull=True)
                | Q(period_end__gte=F("period_start")),
                name="invoice_period_ordered",
            ),
            # `issue_invoices` bills each period of a subscription once, even when it runs twice.
            models.UniqueConstraint(
                fields=("subscription", "period_start"),
                condition=Q(kind="invoice", subscription__isnull=False),
                name="invoice_one_per_subscription_period",
            ),
        )
        indexes = (models.Index(fields=("organisation", "issued_on")),)

    def __str__(self) -> str:
        return self.number or f"draft {self.pk}"


class InvoiceLine(models.Model):
    """One position of an invoice. Frozen with its invoice once that is issued."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveSmallIntegerField(default=1)
    description = models.CharField(max_length=300)
    quantity = models.PositiveIntegerField(default=1)
    unit_net_cents = models.IntegerField(help_text="Net unit price, euro cents.")

    class Meta:
        ordering = ("position", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=("invoice", "position"), name="invoice_line_unique_position"
            ),
        )

    def __str__(self) -> str:
        return f"{self.position}. {self.description}"

    @property
    def net_cents(self) -> int:
        return self.quantity * self.unit_net_cents
