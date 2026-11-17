"""Two database guards for customer data deletion (ADR 0045, issue #85).

Written by hand because Django cannot express triggers — the technique of
`billing/0002_immutable_invoices` and `marceline/0007_rights_logs_append_only`. Both use
SQLSTATE 23000, so Django raises `IntegrityError`.

1. **The deletion record is append-only, UPDATE and DELETE alike.** Unlike the rights logs (whose
   DELETE branch retention needs), a record of a deletion that could itself be deleted answers
   nothing. It holds no personal data — ids, a digest, dates, counts and codes — so there is
   nothing in it to delete for a person's sake.

2. **A rights row on `legal_hold` cannot be deleted at all.** Until now the hold was a Python check
   in `marceline/rights/retention.py`; a hold that a raw `DELETE`, the admin or a future code path
   can step over is not a hold. Nothing in the codebase deletes a held row today, so this changes
   no behaviour — it makes the existing behaviour enforceable. Lift the hold first, deliberately,
   and then delete.
"""

from django.db import migrations

RECORD_TABLE = "marceline_datadeletionrecord"
HOLD_TABLES = (
    "marceline_workrights",
    "marceline_proofdocument",
    "marceline_agencymandate",
    "marceline_rightsschedule",
)

FORWARD = f"""
CREATE FUNCTION marceline_deletion_record_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'data deletion records are append-only' USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER {RECORD_TABLE}_append_only
    BEFORE UPDATE OR DELETE ON {RECORD_TABLE}
    FOR EACH ROW EXECUTE FUNCTION marceline_deletion_record_append_only();

CREATE FUNCTION marceline_refuse_legal_hold_delete() RETURNS trigger AS $$
BEGIN
    IF OLD.legal_hold THEN
        RAISE EXCEPTION 'row % of %: legal hold, lift it before deleting', OLD.id, TG_TABLE_NAME
            USING ERRCODE = '23000';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
""" + "".join(
    f"""
CREATE TRIGGER {table}_legal_hold
    BEFORE DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION marceline_refuse_legal_hold_delete();
"""
    for table in HOLD_TABLES
)

BACKWARD = (
    f"DROP TRIGGER IF EXISTS {RECORD_TABLE}_append_only ON {RECORD_TABLE};\n"
    "DROP FUNCTION IF EXISTS marceline_deletion_record_append_only();\n"
    + "".join(f"DROP TRIGGER IF EXISTS {table}_legal_hold ON {table};\n" for table in HOLD_TABLES)
    + "DROP FUNCTION IF EXISTS marceline_refuse_legal_hold_delete();\n"
)


class Migration(migrations.Migration):
    dependencies = [("marceline", "0018_customer_data_deletion")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
