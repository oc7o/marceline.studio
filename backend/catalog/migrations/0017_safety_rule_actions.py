"""Safety rule actions (operator decisions 2026-09-17, docs/safety.md, ADR 0018 amendment).

`block` becomes `auto_block_and_incident` (every former block term was a minor indicator, and
`safety_incidents` opened a case for each such page anyway). The operator's rules:

1. "rape" (rape, raped, rapes, raping) and "forced": `auto_block`, category `non_consent`.
2. "lolita" (lolita, lolitas): `auto_block_and_incident`, category `minor` (the CSAM category).
3. "flagged term": `customer_release_queue`, category `minor`.

Plain terms: matching is whole words on normalized text (`crawler.safety`), so "grape", "draped"
and "enforced" never match. The operator decided these rules, so they are enabled here.
"""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

RULES: list[tuple[str, str, str]] = [
    # (term, category, action)
    ("rape", "non_consent", "auto_block"),
    ("raped", "non_consent", "auto_block"),
    ("rapes", "non_consent", "auto_block"),
    ("raping", "non_consent", "auto_block"),
    ("forced", "non_consent", "auto_block"),
    ("lolita", "minor", "auto_block_and_incident"),
    ("lolitas", "minor", "auto_block_and_incident"),
    ("flagged term", "minor", "customer_release_queue"),
]
#: Terms that existed before (catalog 0005) with their earlier action; the rest are new here.
PREVIOUS = {
    "rape": "review",
    "raped": "review",
    "forced": "review",
    "lolita": "review",
    "flagged term": "review",
}


def apply_rules(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    SafetyTerm = apps.get_model("catalog", "SafetyTerm")
    SafetyTerm.objects.filter(action="block").update(action="auto_block_and_incident")
    for term, category, action in RULES:
        SafetyTerm.objects.update_or_create(
            term=term,
            defaults={"is_regex": False, "category": category, "action": action, "enabled": True},
        )


def revert_rules(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    SafetyTerm = apps.get_model("catalog", "SafetyTerm")
    for term, _category, _action in RULES:
        if term in PREVIOUS:
            SafetyTerm.objects.filter(term=term).update(action=PREVIOUS[term])
        else:
            SafetyTerm.objects.filter(term=term).delete()
    SafetyTerm.objects.exclude(action="review").update(action="block")


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0016_creator_needs_review'),
    ]

    operations = [
        migrations.CreateModel(
            name='SafetyRuleSet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.CharField(max_length=32, unique=True)),
                ('terms', models.JSONField(default=list, help_text='Enabled terms at that version: term, is_regex, category, action.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.AlterField(
            model_name='safetyterm',
            name='action',
            field=models.CharField(choices=[('review', 'Review (no media until a human clears it)'), ('auto_block', 'Auto-block (no review step, no media, no links)'), ('auto_block_and_incident', 'Auto-block and open a safety incident (CSAM suspect)'), ('customer_release_queue', 'Block; staff may release pages crediting a customer creator')], max_length=32),
        ),
        migrations.RunPython(apply_rules, revert_rules),
    ]
