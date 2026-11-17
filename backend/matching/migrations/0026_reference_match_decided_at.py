"""When a match decision was taken, as its own column (issue #142).

`ReferenceMatch` recorded *who* decided (`decided_by`) and never *when*. `updated_at` is `auto_now`,
so an extent classification or a candidate-source promotion months later moves it — and this date is
snapshotted into a customer's notice draft and printed in the evidence paragraph, where naming a
review later than it happened would overstate what was reviewed.

The backfill takes `updated_at` for every row that is not `proposed`: it is the best record we have
for a decision taken before this column existed, it is never *earlier* than the decision, and every
new decision writes the real moment (`marceline/review/references.py`, `matching/admin.py`,
`matching/references.py`). A `proposed` row is left NULL, because there is no decision to date.
"""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def backfill_from_updated_at(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    reference_match = apps.get_model("matching", "ReferenceMatch")
    reference_match.objects.exclude(status="proposed").filter(decided_at__isnull=True).update(
        decided_at=models.F("updated_at")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("matching", "0025_confidencesettings_calibrated_high_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="referencematch",
            name="decided_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "When the current confirmed/rejected decision was taken. Empty while proposed."
                ),
                null=True,
            ),
        ),
        migrations.RunPython(backfill_from_updated_at, migrations.RunPython.noop, elidable=True),
    ]
