"""Let the GoBD trigger delete an invoice once its statutory retention period has passed.

`0002_immutable_invoices` refused every DELETE of an issued invoice outright ("issue a
cancellation instead"). That was correct while nothing needed to delete one, but retention
(ADR 0032 amendment 2026-09-17: eight years, not the ten the ADR used to say) is a real deletion
policy now, and it must hold in the database like every other GoBD rule here — for the admin, a
shell and raw SQL alike, not only for `billing.retention`.

`billing_invoice_deletable_from(issued_on)` mirrors `billing.retention.deletable_from` in SQL:
the first day of the ninth calendar year after the year of issue (eight full years counted from
the end of the issuing year — § 147 Abs. 3 AO, § 257 Abs. 4 HGB, § 14b Abs. 1 UStG). The DELETE
branch of `billing_invoice_immutable` now allows a DELETE past that date; the UPDATE branch
(immutability of an issued row) is unchanged. `billing_invoiceline_immutable` gets the same
allowance for its DELETE case only, so `ON DELETE CASCADE` from the parent's own deletion can
still remove its lines — INSERT/UPDATE of a line stay refused forever, per CLAUDE.md ("Do not
change invoice numbering or immutability — an issued invoice stays immutable until it is
deleted").

`billing_invoice_today()` is what the trigger compares against, instead of `CURRENT_DATE`
directly: it reads a transaction-local setting (`marceline.retention_today`) and falls back to
the real clock when nothing set it. `billing.retention.apply_retention` sets it (via
`set_config(..., true)`, local to its own transaction) to the same "today" it was told to treat
as current — the real day on every scheduled run, an explicit `--date` only when rehearsing or
catching up, exactly the escape hatch `issue_invoices --date` and `rights_housekeeping --date`
already have. Any other path — the admin, a shell, raw SQL — never sets it, so it falls back to
`CURRENT_DATE` and the real calendar is what protects an invoice from an early delete.

A cancellation (Storno) is issued on or after the invoice it cancels, so its own retention date
never comes earlier; `Invoice.cancels` (`on_delete=PROTECT`) keeps Postgres from deleting an
invoice while its still-retained cancellation exists, exactly as it always has.
"""

from django.db import migrations

FORWARD = r"""
CREATE FUNCTION billing_invoice_deletable_from(issued_on date) RETURNS date AS $$
    SELECT make_date((EXTRACT(YEAR FROM issued_on)::int) + 9, 1, 1);
$$ LANGUAGE sql IMMUTABLE;

-- What `billing.retention.apply_retention` is treating as "today" (set transaction-locally with
-- `set_config`), or the real clock when nothing set it — a raw SQL DELETE, the admin, any other
-- path.
CREATE FUNCTION billing_invoice_today() RETURNS date AS $$
DECLARE
    override text := current_setting('marceline.retention_today', true);
BEGIN
    IF override IS NULL OR override = '' THEN
        RETURN CURRENT_DATE;
    END IF;
    RETURN override::date;
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION billing_invoice_immutable() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.number IS NOT NULL
            AND (OLD.issued_on IS NULL OR billing_invoice_today() < billing_invoice_deletable_from(OLD.issued_on))
        THEN
            RAISE EXCEPTION
                'billing: issued invoice % is retained until % (issue a cancellation instead)',
                OLD.number, billing_invoice_deletable_from(OLD.issued_on)
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

CREATE OR REPLACE FUNCTION billing_invoiceline_immutable() RETURNS trigger AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
        SELECT 1 FROM billing_invoice
        WHERE id = OLD.invoice_id AND number IS NOT NULL
          AND (issued_on IS NULL OR billing_invoice_today() < billing_invoice_deletable_from(issued_on))
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
"""

BACKWARD = r"""
CREATE OR REPLACE FUNCTION billing_invoiceline_immutable() RETURNS trigger AS $$
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

CREATE OR REPLACE FUNCTION billing_invoice_immutable() RETURNS trigger AS $$
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

DROP FUNCTION IF EXISTS billing_invoice_today();
DROP FUNCTION IF EXISTS billing_invoice_deletable_from(date);
"""


class Migration(migrations.Migration):
    dependencies = [("billing", "0003_invoice_public_id")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
