"""Database defaults for the columns a worker process may not know about yet.

Metadata-only `ALTER TABLE ... SET DEFAULT`: no table rewrite, no data change. Why: a container
still running the code from before a migration inserts without the new column, and a NOT NULL
column with only a Python default then rejects the row (2026-09-15: 997 detail pages lost to
`null value in column "availability"`). Pinned by
`marceline/tests/test_stale_worker_writes.py`.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crawler", "0005_alter_sourcecandidate_kind_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="crawltask",
            name="attempts",
            field=models.PositiveSmallIntegerField(db_default=0, default=0),
        ),
        migrations.AlterField(
            model_name="crawltask",
            name="context",
            field=models.JSONField(
                blank=True,
                db_default={},
                default=dict,
                help_text="Hints from the linking page, e.g. listing thumbnails.",
            ),
        ),
        migrations.AlterField(
            model_name="crawltask",
            name="priority",
            field=models.IntegerField(db_default=0, default=0),
        ),
        migrations.AlterField(
            model_name="crawltask",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("running", "Running"),
                    ("done", "Done"),
                    ("failed", "Failed"),
                    ("skipped", "Skipped"),
                ],
                db_default="pending",
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="llmcall",
            name="cost_usd",
            field=models.DecimalField(db_default=0, decimal_places=6, default=0, max_digits=10),
        ),
        migrations.AlterField(
            model_name="llmcall",
            name="input_tokens",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.AlterField(
            model_name="llmcall",
            name="output_tokens",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
    ]
