"""Every change to a policy-bearing `Site` field is recorded, by the database (issue #106).

Written by hand because Django cannot express triggers, and because a *model-layer* audit would have
missed the change that prompted this one: on 2026-09-18 `Site.capture_play_click` was flipped with a
queryset `.update()`, which fires no `pre_save`/`post_save` signal at all. A trigger covers the
admin, a management command, a shell `.save()`, a data migration, `.update()` and raw SQL with one
piece of code — the same reason the invoices (`billing/migrations/0002_immutable_invoices`), the
rights and notice logs and the suppressed-creator link are guarded this way.

Three pieces:

1. `catalog_age_confirmation_shape(jsonb)` — the *shape* of an age approval (`empty`,
   `approval, N cookie(s)`). The audit never stores a cookie name or value; `catalog.policy_audit.
   age_confirmation_shape` is the Python copy of this, and a test compares the two.
2. `catalog_site_policy_audit()` on `AFTER UPDATE OF … ON catalog_site` — one row per changed field.
   The actor comes from the transaction-local setting `marceline.policy_actor`
   (`catalog.policy_audit.policy_actor`), the reason from `marceline.policy_reason`; with neither
   set the row reads `database`, which is honest and is itself a finding. `paused_until` and
   `pause_reason` are recorded **only** when an actor is set: the crawler writes them on every
   outage and every reservation pause, and burying human decisions under that bookkeeping would
   defeat the table.
3. `catalog_sitepolicychange` is append-only: UPDATE and DELETE are refused (SQLSTATE 23000 →
   `IntegrityError`). A `Site` that carries audit rows can therefore not be deleted — which is the
   intended consequence and breaks nothing: the only code that deletes a `Site` is
   `crawler.discovery.services.undo_approval`, and it already refuses a site with any related row.
"""

from django.db import migrations

TRACKED = (
    "enabled",
    "capture_enabled",
    "capture_play_click",
    "crawl_delay_s",
    "daily_request_budget",
    "daily_media_request_budget",
)
HUMAN_ONLY = ("paused_until", "pause_reason")
#: The columns the trigger watches, so an unrelated write (a crawl delay observation, a failure
#: counter) does not even fire it.
WATCHED = TRACKED + HUMAN_ONLY + ("age_confirmation",)

FORWARD = f"""
CREATE FUNCTION catalog_age_confirmation_shape(value jsonb) RETURNS text AS $$
DECLARE
    cookies integer := 0;
BEGIN
    IF value IS NULL OR value = 'null'::jsonb OR value = '{{}}'::jsonb THEN
        RETURN 'empty';
    END IF;
    IF jsonb_typeof(value -> 'cookies') = 'object' THEN
        SELECT count(*) INTO cookies FROM jsonb_object_keys(value -> 'cookies');
    END IF;
    RETURN 'approval, ' || cookies || ' cookie(s)';
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE FUNCTION catalog_site_policy_audit() RETURNS trigger AS $$
DECLARE
    named text := nullif(current_setting('marceline.policy_actor', true), '');
    actor text := coalesce(named, 'database');
    reason text := coalesce(current_setting('marceline.policy_reason', true), '');
    fields text[] := ARRAY[{", ".join(f"'{name}'" for name in TRACKED)}];
    old_row jsonb := to_jsonb(OLD);
    new_row jsonb := to_jsonb(NEW);
    name text;
    was text;
    now_value text;
BEGIN
    IF named IS NOT NULL THEN
        fields := fields || ARRAY[{", ".join(f"'{name}'" for name in HUMAN_ONLY)}];
    END IF;
    FOREACH name IN ARRAY fields LOOP
        was := old_row ->> name;
        now_value := new_row ->> name;
        IF was IS DISTINCT FROM now_value THEN
            INSERT INTO catalog_sitepolicychange
                (site_id, field, old_value, new_value, actor, reason, changed_at)
            VALUES (NEW.id, name, coalesce(was, ''), coalesce(now_value, ''), actor, reason, now());
        END IF;
    END LOOP;
    IF OLD.age_confirmation IS DISTINCT FROM NEW.age_confirmation THEN
        INSERT INTO catalog_sitepolicychange
            (site_id, field, old_value, new_value, actor, reason, changed_at)
        VALUES (
            NEW.id,
            'age_confirmation',
            catalog_age_confirmation_shape(OLD.age_confirmation),
            catalog_age_confirmation_shape(NEW.age_confirmation),
            actor,
            reason,
            now()
        );
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER catalog_site_policy_audit
    AFTER UPDATE OF {", ".join(WATCHED)} ON catalog_site
    FOR EACH ROW EXECUTE FUNCTION catalog_site_policy_audit();

CREATE FUNCTION catalog_site_policy_change_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'site policy changes are append-only' USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER catalog_sitepolicychange_append_only
    BEFORE UPDATE OR DELETE ON catalog_sitepolicychange
    FOR EACH ROW EXECUTE FUNCTION catalog_site_policy_change_append_only();
"""

BACKWARD = """
DROP TRIGGER IF EXISTS catalog_sitepolicychange_append_only ON catalog_sitepolicychange;
DROP FUNCTION IF EXISTS catalog_site_policy_change_append_only();
DROP TRIGGER IF EXISTS catalog_site_policy_audit ON catalog_site;
DROP FUNCTION IF EXISTS catalog_site_policy_audit();
DROP FUNCTION IF EXISTS catalog_age_confirmation_shape(jsonb);
"""


class Migration(migrations.Migration):
    dependencies = [("catalog", "0023_site_robots_policy_and_policy_audit")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
