"""The metadata candidate as a browser-capture reason (ADR 0040 amendment 2026-09-18).

Choices only: `reason` is a plain CharField, so nothing in the database changes. The third signal
stops writing review-band `ReferenceMatch` rows and becomes one more source of the capture queue,
which is why its reason code has to exist here.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("matching", "0022_reference_crop_grid"),
    ]

    operations = [
        migrations.AlterField(
            model_name="browsercapturecandidate",
            name="reason",
            field=models.CharField(
                choices=[
                    ("review_band_match", "Review Band Match"),
                    ("confirmed_leak_without_frames", "Confirmed Leak Without Frames"),
                    ("linked_cluster_member", "Linked Cluster Member"),
                    ("exact_duration_single_thumb", "Exact Duration Single Thumb"),
                    ("metadata_candidate", "Metadata Candidate"),
                ],
                help_text="Leading source.",
                max_length=32,
            ),
        ),
    ]
