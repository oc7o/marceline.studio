"""The notice event log is append-only (issue #33, ADR 0035): no UPDATE on a `NoticeEvent`, ever.

Same technique as `0007_rights_logs_append_only.py` (Django cannot express triggers). DELETE stays
possible in principle (nothing uses it yet — notice drafts have no retention job in this change);
the admin offers no delete either way. Errors use SQLSTATE 23000 -> `IntegrityError`.
"""

from django.db import migrations

TABLE = "marceline_noticeevent"

FORWARD = f"""
CREATE FUNCTION marceline_notice_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'notice log %: rows are append-only', TG_TABLE_NAME USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER {TABLE}_append_only
    BEFORE UPDATE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION marceline_notice_log_append_only();
"""

BACKWARD = f"""
DROP TRIGGER IF EXISTS {TABLE}_append_only ON {TABLE};
DROP FUNCTION IF EXISTS marceline_notice_log_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [("marceline", "0010_notices")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
