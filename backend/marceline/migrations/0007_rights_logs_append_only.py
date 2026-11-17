"""Rights logs are append-only (ADR 0034): no UPDATE on a transition or an event, ever.

Written by hand because Django cannot express triggers (the same technique as
`billing/migrations/0002_immutable_invoices`). DELETE stays possible: the retention job removes a
closed record together with its log once the retention period is over, and nothing else deletes
them (the admin offers no delete for either table). Errors use SQLSTATE 23000 → `IntegrityError`.
"""

from django.db import migrations

TABLES = ("marceline_workrightstransition", "marceline_rightsevent")

FORWARD = r"""
CREATE FUNCTION marceline_rights_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'rights log %: rows are append-only', TG_TABLE_NAME USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;
""" + "".join(
    f"""
CREATE TRIGGER {table}_append_only
    BEFORE UPDATE ON {table}
    FOR EACH ROW EXECUTE FUNCTION marceline_rights_log_append_only();
"""
    for table in TABLES
)

BACKWARD = (
    "".join(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};\n" for table in TABLES)
    + "DROP FUNCTION IF EXISTS marceline_rights_log_append_only();\n"
)


class Migration(migrations.Migration):
    dependencies = [("marceline", "0006_rights")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
