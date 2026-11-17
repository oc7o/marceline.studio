"""GoBD in the database: issued invoices and their numbers cannot be changed or taken back.

Written by hand because Django cannot express triggers. The Python services refuse the same
things first (`billing.services`); these triggers make the rule hold for the admin, a shell, a
data migration and raw SQL alike. Errors use SQLSTATE 23000, which Django raises as
`IntegrityError`.

- `billing_invoice`: once `number` is set, every column except `paid_on` and `payment_note` is
  frozen, and the row cannot be deleted.
- `billing_invoiceline`: no line of an issued invoice can be inserted, changed or deleted (and no
  line can be moved onto one).
- `billing_invoicesequence`: a counter never goes down and is never deleted, so a number is never
  handed out twice.
"""

from django.db import migrations

FORWARD = r"""
CREATE FUNCTION billing_invoice_immutable() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.number IS NOT NULL THEN
            RAISE EXCEPTION 'billing: issued invoice % cannot be deleted; issue a cancellation', OLD.number
                USING ERRCODE = '23000';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.number IS NOT NULL
        AND (to_jsonb(OLD) - 'paid_on' - 'payment_note')
            IS DISTINCT FROM (to_jsonb(NEW) - 'paid_on' - 'payment_note') THEN
        RAISE EXCEPTION 'billing: issued invoice % is immutable; only payment fields may change', OLD.number
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER billing_invoice_immutable
    BEFORE UPDATE OR DELETE ON billing_invoice
    FOR EACH ROW EXECUTE FUNCTION billing_invoice_immutable();

CREATE FUNCTION billing_invoiceline_immutable() RETURNS trigger AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
        SELECT 1 FROM billing_invoice WHERE id = OLD.invoice_id AND number IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'billing: lines of an issued invoice are immutable' USING ERRCODE = '23000';
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
        SELECT 1 FROM billing_invoice WHERE id = NEW.invoice_id AND number IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'billing: lines of an issued invoice are immutable' USING ERRCODE = '23000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER billing_invoiceline_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON billing_invoiceline
    FOR EACH ROW EXECUTE FUNCTION billing_invoiceline_immutable();

CREATE FUNCTION billing_invoicesequence_monotonic() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'billing: invoice number sequences are never deleted' USING ERRCODE = '23000';
    END IF;
    IF NEW.year <> OLD.year OR NEW.last_number < OLD.last_number THEN
        RAISE EXCEPTION 'billing: invoice numbers are never reused' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER billing_invoicesequence_monotonic
    BEFORE UPDATE OR DELETE ON billing_invoicesequence
    FOR EACH ROW EXECUTE FUNCTION billing_invoicesequence_monotonic();
"""

BACKWARD = r"""
DROP TRIGGER IF EXISTS billing_invoicesequence_monotonic ON billing_invoicesequence;
DROP FUNCTION IF EXISTS billing_invoicesequence_monotonic();
DROP TRIGGER IF EXISTS billing_invoiceline_immutable ON billing_invoiceline;
DROP FUNCTION IF EXISTS billing_invoiceline_immutable();
DROP TRIGGER IF EXISTS billing_invoice_immutable ON billing_invoice;
DROP FUNCTION IF EXISTS billing_invoice_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [("billing", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
