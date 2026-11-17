"""Give every invoice a public id (`inv_…`, ADR 0033) without touching an issued invoice's row.

Issued invoices are frozen by the `billing_invoice_immutable` trigger (`0002`): a `BEFORE UPDATE`
row trigger that refuses any change but the payment fields. A classic backfill — add a nullable
column, `UPDATE` each row, then make it unique and `NOT NULL` — would therefore fail on the first
issued invoice, and disabling the trigger for it would break the very guarantee GoBD asks for.

So there is no `UPDATE` at all. The column is added **with its database default**
(`RandomPublicId`, a volatile `gen_random_uuid()` expression): Postgres evaluates it once per
existing row while it rewrites the table inside `ALTER TABLE … ADD COLUMN`, and a table rewrite
fires no row-level triggers. The unique index is built in the same statement. The `RunSQL` after it
checks the outcome, so a future change that turns this into an `UPDATE` fails loudly here rather
than half-applied.

From then on the id is part of the frozen row: the trigger compares whole rows except the payment
fields, so an issued invoice's public id can never change either
(`billing/tests/test_invoices.py`).
"""

import functools

from django.db import migrations, models

import core.public_ids

CHECK = r"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM billing_invoice WHERE public_id IS NULL OR public_id !~ '^inv_[0-9a-f]{32}$') THEN
        RAISE EXCEPTION 'billing: an invoice has no well-formed public id after 0003';
    END IF;
END;
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0002_immutable_invoices"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="public_id",
            field=models.CharField(
                db_default=core.public_ids.RandomPublicId("inv"),
                default=functools.partial(core.public_ids.generate, "inv"),
                editable=False,
                help_text=(
                    "Opaque id shown to customers instead of the primary key (ADR 0033). "
                    "Never reused."
                ),
                max_length=48,
                unique=True,
            ),
        ),
        migrations.RunSQL(CHECK, migrations.RunSQL.noop),
    ]
