"""An `expire` purge's issue time, so a bundle rendered before it cannot undo it (ADR 0028)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("delivery", "0003_alter_outboxitem_kind")]

    operations = [
        migrations.AddField(
            model_name="cachedbundle",
            name="expire_requested_as_of",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "The newest `expire` purge's signed issue time (dev clock). A bundle rendered "
                    "at or before it is stored already expired, so a fetch that raced the purge "
                    "cannot undo it — the `expire` counterpart of `CacheTombstone`."
                ),
                null=True,
            ),
        )
    ]
