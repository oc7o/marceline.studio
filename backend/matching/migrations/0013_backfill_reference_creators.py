"""Backfill Reference.creators from the primary Reference.creator (issue #64).

Until now an original had exactly one creator, so the complete performer list of an existing row
is its primary creator alone. Co-performers that operators noted as free text in `source_note` are
deliberately NOT parsed here: the note has no format, and guessing names would link the wrong
creators. An operator adds them by hand in the admin (Originals -> Creators) or re-imports the
original with a repeatable `--creator`.
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

BATCH = 500


def link_primary_creators(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    Reference = apps.get_model("matching", "Reference")
    through = Reference.creators.through
    rows = [
        through(reference_id=pk, creator_id=creator_id)
        for pk, creator_id in Reference.objects.values_list("pk", "creator_id")
    ]
    through.objects.bulk_create(rows, batch_size=BATCH, ignore_conflicts=True)


def unlink_all(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Reverse: the join table only ever held data this migration can rebuild."""
    Reference = apps.get_model("matching", "Reference")
    Reference.creators.through.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("matching", "0012_reference_creators_alter_reference_creator"),
    ]

    operations = [
        migrations.RunPython(link_primary_creators, unlink_all),
    ]
