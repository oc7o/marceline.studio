# The customer's own authority declaration, and the rename of `verified` (ADR 0034 amendment
# 2026-09-18). The data migration touches `WorkRights.status` only: `WorkRightsTransition` refuses
# every UPDATE by trigger, and rightly so — the log says what was recorded at the time, under the
# semantics of that time.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def rename_verified(apps, schema_editor):
    """`verified` meant "staff read the documents and decided they support the claim"; it now
    means "the documents are formally complete". Same rows, honest name."""
    apps.get_model("marceline", "WorkRights").objects.filter(status="verified").update(
        status="documents_complete"
    )


def back_to_verified(apps, schema_editor):
    apps.get_model("marceline", "WorkRights").objects.filter(
        status__in=("documents_complete", "authority_declared")
    ).update(status="verified")


class Migration(migrations.Migration):

    dependencies = [
        ('marceline', '0014_mandate_formal_check_help_text'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='workrights',
            name='authority_declaration_language',
            field=models.CharField(blank=True, max_length=8),
        ),
        migrations.AddField(
            model_name='workrights',
            name='authority_declaration_text',
            field=models.TextField(blank=True, help_text='The exact text the customer confirmed, as shown.'),
        ),
        migrations.AddField(
            model_name='workrights',
            name='authority_declaration_version',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name='workrights',
            name='authority_declared_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='workrights',
            name='authority_declared_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='workrights',
            name='status',
            field=models.CharField(choices=[('unverified', 'Unverified'), ('self_declared', 'self-declared'), ('documents_received', 'documents received'), ('documents_complete', 'documents complete (formal check)'), ('authority_declared', 'authority declared by the customer'), ('disputed', 'Disputed'), ('revoked', 'Revoked'), ('expired', 'Expired'), ('superseded', 'Superseded')], default='unverified', max_length=24),
        ),
        migrations.AlterField(
            model_name='workrightstransition',
            name='from_status',
            field=models.CharField(blank=True, choices=[('unverified', 'Unverified'), ('self_declared', 'self-declared'), ('documents_received', 'documents received'), ('documents_complete', 'documents complete (formal check)'), ('authority_declared', 'authority declared by the customer'), ('disputed', 'Disputed'), ('revoked', 'Revoked'), ('expired', 'Expired'), ('superseded', 'Superseded')], max_length=24),
        ),
        migrations.AlterField(
            model_name='workrightstransition',
            name='to_status',
            field=models.CharField(choices=[('unverified', 'Unverified'), ('self_declared', 'self-declared'), ('documents_received', 'documents received'), ('documents_complete', 'documents complete (formal check)'), ('authority_declared', 'authority declared by the customer'), ('disputed', 'Disputed'), ('revoked', 'Revoked'), ('expired', 'Expired'), ('superseded', 'Superseded')], max_length=24),
        ),
        migrations.RunPython(rename_verified, back_to_verified),
    ]
