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
        ("index", "0018_safety_incident_hash"),
    ]

    operations = [
        migrations.AlterField(
            model_name="framehash",
            name="frame_index",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.AlterField(
            model_name="media",
            name="attempts",
            field=models.PositiveSmallIntegerField(db_default=0, default=0),
        ),
        migrations.AlterField(
            model_name="media",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("running", "Running"),
                    ("done", "Done"),
                    ("failed", "Failed"),
                    ("skipped", "Skipped"),
                    ("robots_blocked", "Robots Blocked"),
                ],
                db_default="pending",
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="page",
            name="active",
            field=models.BooleanField(
                db_default=True,
                db_index=True,
                default=True,
                help_text="Considered by matching and rechecks. Inactive pages are kept, not queried.",
            ),
        ),
        migrations.AlterField(
            model_name="page",
            name="availability",
            field=models.CharField(
                choices=[("playable", "Playable"), ("removed", "Removed"), ("unknown", "Unknown")],
                db_default="unknown",
                db_index=True,
                default="unknown",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="page",
            name="raw_extraction",
            field=models.JSONField(
                blank=True,
                db_default={},
                default=dict,
                help_text="What the LLM or parser returned.",
            ),
        ),
        migrations.AlterField(
            model_name="page",
            name="safety_matches",
            field=models.JSONField(
                blank=True,
                db_default=[],
                default=list,
                help_text="Matched safety terms: term, category, action.",
            ),
        ),
        migrations.AlterField(
            model_name="page",
            name="safety_release",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "—"),
                    ("queued", "Queued for staff (customer credited)"),
                    ("released", "Released for fingerprinting by staff"),
                    ("kept_blocked", "Kept blocked by staff"),
                    ("revoked", "Release revoked (another safety term matched)"),
                ],
                db_default="",
                db_index=True,
                default="",
                help_text="Customer release queue state (docs/safety.md). Only `released` lets a page blocked by a customer_release_queue term become clear; any other safety hit revokes it.",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="page",
            name="safety_score_signals",
            field=models.JSONField(
                blank=True,
                db_default=[],
                default=list,
                help_text="Every scoring signal that fired, for review.",
            ),
        ),
        migrations.AlterField(
            model_name="page",
            name="safety_status",
            field=models.CharField(
                choices=[("clear", "Clear"), ("review", "Review"), ("blocked", "Blocked")],
                db_default="clear",
                db_index=True,
                default="clear",
                help_text="Text-based content-safety flag. Blocked pages: follow docs/safety.md.",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="page",
            name="tags",
            field=models.JSONField(blank=True, db_default=[], default=list),
        ),
        migrations.AlterField(
            model_name="safetyincident",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "Open"),
                    ("reported", "Reported"),
                    ("closed", "Closed"),
                    ("false_positive", "False Positive"),
                ],
                db_default="open",
                db_index=True,
                default="open",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="safetyincident",
            name="terms",
            field=models.JSONField(
                blank=True,
                db_default=[],
                default=list,
                help_text="Every {category, term} this case was flagged for, including later term hits.",
            ),
        ),
        migrations.AlterField(
            model_name="safetyruledecision",
            name="terms",
            field=models.JSONField(
                blank=True,
                db_default=[],
                default=list,
                help_text="Matched rule terms: term, category, action.",
            ),
        ),
        migrations.AlterField(
            model_name="sitecreatorbreadcrumb",
            name="page_count",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
    ]
