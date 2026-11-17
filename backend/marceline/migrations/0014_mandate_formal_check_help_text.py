# Help text only (ADR 0034 amendment 2026-09-18): what staff are asked about a mandate is a
# formal-completeness check, never what the document legally means. No column changes.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marceline', '0013_remove_reportdecision_report_delete_report_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='agencymandate',
            name='authority_to_prepare_notices',
            field=models.BooleanField(default=False, help_text='The document names notices expressly: it says in its own words that the agent may prepare or submit takedown notices. A presence check, not a reading of what the wording covers — if it takes interpretation to answer, leave this unticked.'),
        ),
        migrations.AlterField(
            model_name='agencymandate',
            name='principal_verified',
            field=models.BooleanField(default=False, help_text="Staff checked the principal's identity, the same formal check as for an owner. Never a statement about the principal's rights."),
        ),
        migrations.AlterField(
            model_name='agencymandate',
            name='rights_covered',
            field=models.TextField(help_text='The rights the document itself names, transcribed. Not an assessment of what it grants.'),
        ),
    ]
