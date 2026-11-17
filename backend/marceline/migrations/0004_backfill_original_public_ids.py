"""Give every owned original its public id (issue #77, ADR 0033).

A customer original is one file (sha256) as one organisation owns it, so the backfill writes one
`OriginalPublicId` per distinct (organisation, sha256) among the ownership rows. Rows that already
exist are kept, so running it again changes nothing, and each new row gets a fresh random id from
the field's default. Organisations' and pages' public ids need no data migration: their columns
were added with a database default that fills every existing row.

The count is printed, like the ownership backfill's, so `make migrate` shows what it wrote.
"""

from typing import Any

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

BATCH = 500


def backfill(ownership_model: Any, public_id_model: Any) -> dict[str, int]:
    """One public id per owned file per organisation; return what happened."""
    owned = set(
        ownership_model.objects.values_list("organisation_id", "reference__sha256").distinct()
    )
    existing = set(public_id_model.objects.values_list("organisation_id", "sha256"))
    missing = sorted(owned - existing)
    public_id_model.objects.bulk_create(
        [public_id_model(organisation_id=org, sha256=sha256) for org, sha256 in missing],
        batch_size=BATCH,
    )
    return {"created": len(missing), "already_present": len(owned & existing)}


def forward(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    counts = backfill(
        apps.get_model("marceline", "OriginalOwnership"),
        apps.get_model("marceline", "OriginalPublicId"),
    )
    print(
        "\n  original public id backfill: "
        + ", ".join(f"{name.replace('_', ' ')} {value}" for name, value in counts.items())
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marceline", "0003_originalpublicid"),
        ("matching", "0018_reference_unique_sha256_pipeline_fps"),
    ]

    operations = [
        # Reversing drops the table in 0003; nothing to undo here.
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
