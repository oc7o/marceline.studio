# ADR 0041, issue #22: who produced a page's fields — the site's adapter, or a model repairing
# for it. Numbered 0023 and depending on both current leaves: 0021 (the suppression trigger) and
# 0022 (frame embeddings) were renumbered past each other and never joined.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        # Behind the sprite-grid columns, which became the index app's leaf when issue #14
        # merged into main while this branch was in flight. 0021 and 0022_framehash_embedding
        # are both ancestors of it, so naming them again would say nothing.
        ('index', '0022_media_sprite_columns_media_sprite_rows_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='page',
            name='extraction_source',
            field=models.CharField(choices=[('adapter', 'Adapter'), ('llm_repair', 'LLM repair')], db_default='adapter', db_index=True, default='adapter', help_text="Who produced this page's fields: the site's adapter, or a repairing model.", max_length=16),
        ),
    ]
