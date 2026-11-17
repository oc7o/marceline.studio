# The `exported` notice event (issue #33, ADR 0035 amendment 2026-09-18): the customer downloaded
# the approved text as a file. Choices only — no column changes, and the log stays append-only.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marceline", "0015_rights_authority_declaration"),
    ]

    operations = [
        migrations.AlterField(
            model_name="noticeevent",
            name="kind",
            field=models.CharField(
                choices=[
                    ("created", "Created"),
                    ("finding_added", "Finding Added"),
                    ("finding_removed", "Finding Removed"),
                    ("approved", "Approved"),
                    ("exported", "Exported"),
                    ("marked_sent", "Marked Sent"),
                    ("sandbox_sent", "Sandbox Sent"),
                    ("provider_response_recorded", "Provider Response Recorded"),
                    ("withdrawn", "Withdrawn"),
                    ("corrected", "Corrected"),
                ],
                max_length=32,
            ),
        ),
    ]
