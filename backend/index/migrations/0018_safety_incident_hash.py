import django.db.models.deletion
import django.db.models.functions.text
import django.db.models.lookups
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('index', '0017_safety_release_and_rule_decisions'),
    ]

    operations = [
        migrations.AlterField(
            model_name='safetyincidentpage',
            name='evidence',
            field=models.CharField(choices=[('origin', 'Origin'), ('same_url', 'Same Url'), ('term_change', 'Term Change'), ('mirror', 'Mirror'), ('sha256', 'Sha256'), ('cluster', 'Cluster'), ('pdq', 'Pdq'), ('manual', 'Manual')], help_text='Why this page belongs to this case.', max_length=16),
        ),
        migrations.CreateModel(
            name='SafetyIncidentHash',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('pdq', models.BinaryField(max_length=32)),
                ('quality', models.PositiveSmallIntegerField(help_text='PDQ quality score, 0-100.')),
                ('source_kind', models.CharField(blank=True, help_text='Media kind the hash was computed from. No URL, no bytes, no page content.', max_length=16)),
                ('retained_at', models.DateTimeField(auto_now_add=True)),
                ('incident', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='retained_hashes', to='index.safetyincident')),
            ],
            options={
                'verbose_name_plural': 'safety incident hashes',
                'constraints': [models.UniqueConstraint(fields=('incident', 'pdq'), name='uniq_incident_pdq'), models.CheckConstraint(condition=django.db.models.lookups.Exact(django.db.models.functions.text.Length('pdq'), 32), name='incidenthash_pdq_32_bytes'), models.CheckConstraint(condition=models.Q(('quality__lte', 100)), name='incidenthash_quality')],
            },
        ),
    ]
