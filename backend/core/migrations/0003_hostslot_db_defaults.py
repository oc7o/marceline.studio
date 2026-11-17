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
        ("core", "0002_hostslot"),
    ]

    operations = [
        migrations.AlterField(
            model_name="hostslot",
            name="last_delay_s",
            field=models.FloatField(
                db_default=0.0, default=0.0, help_text="Delay the last request asked for."
            ),
        ),
        migrations.AlterField(
            model_name="hostslot",
            name="min_delay_s",
            field=models.FloatField(
                db_default=0.0,
                default=0.0,
                help_text="Operator floor for every request to this host, in seconds (0: callers decide).",
            ),
        ),
    ]
