"""A suppressed name is never linked to a page again — enforced in the database.

Index suppression (docs/suppression.md) must survive a re-crawl: once a performer who is not our
customer has told us to stop indexing them, no crawl, seed, backfill, admin action or future
writer may re-create the association, not even when the page is crawled again for somebody else
it credits.

`Creator.objects.usable()` keeps suppressed creators out of every reading path, which is where
this is *decided*. This trigger is the last line of defence for the one table that holds the
association itself: a BEFORE INSERT that returns NULL, so the row is silently not stored. Silent
is right here — the crawler's job is to store the page, and it must not fail because one of the
performers a page credits is suppressed.

Same technique as `marceline/0007_rights_logs_append_only.py` (Django cannot express triggers).
Lifting a suppression sets `lifted_at`, and links may be stored again from that moment.
"""

from django.db import migrations

TABLE = "index_page_creators"

FORWARD = f"""
CREATE FUNCTION index_drop_suppressed_creator_link() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM catalog_creatorsuppression
        WHERE creator_id = NEW.creator_id AND lifted_at IS NULL
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER {TABLE}_suppressed
    BEFORE INSERT ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION index_drop_suppressed_creator_link();
"""

BACKWARD = f"""
DROP TRIGGER IF EXISTS {TABLE}_suppressed ON {TABLE};
DROP FUNCTION IF EXISTS index_drop_suppressed_creator_link();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0018_creator_suppression"),
        ("index", "0022_framehash_embedding"),
    ]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
