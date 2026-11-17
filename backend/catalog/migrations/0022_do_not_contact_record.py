# Hand-written for issue #98 / ADR 0047 (the do-not-contact record), in the shape makemigrations
# produces: one new table, no change to any existing one.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0021_site_capture_play_click"),
    ]

    operations = [
        migrations.CreateModel(
            name="DoNotContactRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "identifier_kind",
                    models.CharField(
                        choices=[
                            ("email", "Email address"),
                            ("postal", "Postal address (the whole address, as written)"),
                            ("organisation", "Company or studio name plus its city"),
                            ("handle", "Social handle, as `platform:handle` or a profile URL"),
                            ("phone", "Telephone number, exactly as it was given"),
                        ],
                        help_text="What kind of identifier was recorded. It is part of the hash, "
                        "so a record of one kind never blocks another.",
                        max_length=16,
                    ),
                ),
                (
                    "identifier_hash",
                    models.CharField(
                        editable=False,
                        help_text="HMAC-SHA256 of the normalised identifier. Not reversible, and "
                        "useless without the server-side key.",
                        max_length=64,
                        unique=True,
                    ),
                ),
                (
                    "received_on",
                    models.DateField(help_text="When the 'no' reached us (day precision)."),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[
                            ("letter", "Letter or postcard"),
                            ("email", "Email"),
                            ("phone", "Telephone"),
                            ("in_person", "In person (trade fair, meeting)"),
                            ("platform_message", "Message on a platform"),
                            ("third_party", "Through an agency, a studio or a lawyer"),
                            ("other", "Other — see the note"),
                        ],
                        help_text="How it reached us.",
                        max_length=24,
                    ),
                ),
                (
                    "recorded_by",
                    models.CharField(
                        help_text="Who recorded it (username or 'operator').", max_length=150
                    ),
                ),
                (
                    "note",
                    models.TextField(
                        blank=True,
                        help_text="One line for staff, e.g. 'objection to the letter of <date>'. "
                        "No names, no contact details, no quote from the message.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "creator",
                    models.ForeignKey(
                        blank=True,
                        help_text="The prospect row this 'no' came from, if there is one. "
                        "Optional, and deleting that row leaves this one standing.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="do_not_contact_records",
                        to="catalog.creator",
                    ),
                ),
            ],
            options={
                "verbose_name": "do-not-contact record",
                "ordering": ("-received_on", "-id"),
                "indexes": [
                    models.Index(fields=["identifier_kind"], name="do_not_contact_kind_idx")
                ],
            },
        ),
    ]
