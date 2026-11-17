"""Backfill `OriginalOwnership` from the rule it replaces (issue #76, ADR 0031).

Until now an organisation owned an original through `Reference.uploaded_by` -> that user's
`Membership` -> the organisation. This migration writes that answer down once, so that what a
customer sees does not change on the day ownership becomes a column:

- an original whose uploader has a membership gets a row for that organisation (`backfill`);
- an original with no uploader (operator/CLI import) or whose uploader has no membership stays
  unassigned — no row, invisible to customers, exactly as before;
- an original that already has a row is left alone, so running the rule again changes nothing.

Deterministic (references in primary-key order, one membership per user by constraint) and
idempotent. The counts are printed, because "how many originals did the migration assign" is the
first thing to check against the customer view after `make migrate`.
"""

from typing import Any

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.utils import timezone

BATCH = 500
VIA = "backfill"


def backfill(reference_model: Any, membership_model: Any, ownership_model: Any) -> dict[str, int]:
    """Assign every resolvable original; return what happened, per outcome."""
    organisation_of_user = dict(membership_model.objects.values_list("user_id", "organisation_id"))
    already = set(ownership_model.objects.values_list("reference_id", flat=True))
    counts = {"assigned": 0, "already_owned": 0, "no_uploader": 0, "uploader_without_membership": 0}
    now = timezone.now()
    rows = []
    for pk, uploader_id in reference_model.objects.order_by("pk").values_list("pk", "uploaded_by_id"):
        if pk in already:
            counts["already_owned"] += 1
        elif uploader_id is None:
            counts["no_uploader"] += 1
        elif uploader_id not in organisation_of_user:
            counts["uploader_without_membership"] += 1
        else:
            counts["assigned"] += 1
            rows.append(
                ownership_model(
                    reference_id=pk,
                    organisation_id=organisation_of_user[uploader_id],
                    assigned_via=VIA,
                    assigned_at=now,
                )
            )
    ownership_model.objects.bulk_create(rows, batch_size=BATCH)
    return counts


def forward(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    counts = backfill(
        apps.get_model("matching", "Reference"),
        apps.get_model("accounts", "Membership"),
        apps.get_model("marceline", "OriginalOwnership"),
    )
    print(
        "\n  original ownership backfill: "
        + ", ".join(f"{name.replace('_', ' ')} {value}" for name, value in counts.items())
    )


def reverse(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Only rows this migration wrote; deliberate assignments made since are not its to undo."""
    apps.get_model("marceline", "OriginalOwnership").objects.filter(assigned_via=VIA).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marceline", "0001_initial"),
        ("accounts", "0002_loginattempt"),
        ("matching", "0018_reference_unique_sha256_pipeline_fps"),
    ]

    operations = [
        migrations.RunPython(forward, reverse),
    ]
