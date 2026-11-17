"""Conservative starter set of content-safety terms (docs/safety.md). Edit them in the admin.

`block`: clear indicators of minors. `review`: ambiguous words, often role play or staged content;
a human decides. "teen" alone is deliberately absent: it is commonly used for people aged
18-19. Existing terms are left untouched.
"""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

# Ages 1-17 followed by "yo", "y.o.", "yr(s) old" or "year(s) old"; text is already normalized.
UNDERAGE_AGE_REGEX = r"(?:[1-9]|1[0-7]) ?(?:y ?o|yrs? old|years? old)"

TERMS: list[tuple[str, bool, str, str]] = [
    # (term, is_regex, category, action)
    ("underage", False, "minor", "block"),
    ("under age", False, "minor", "block"),
    ("preteen", False, "minor", "block"),
    ("pre teen", False, "minor", "block"),
    ("child porn", False, "minor", "block"),
    ("childporn", False, "minor", "block"),
    ("cp video", False, "minor", "block"),
    ("pthc", False, "minor", "block"),
    ("jailbait", False, "minor", "block"),
    ("loli", False, "minor", "block"),
    ("shota", False, "minor", "block"),
    (UNDERAGE_AGE_REGEX, True, "minor", "block"),
    ("lolita", False, "minor", "review"),
    ("flagged term", False, "minor", "review"),
    ("rape", False, "non_consent", "review"),
    ("raped", False, "non_consent", "review"),
    ("drugged", False, "non_consent", "review"),
    ("passed out", False, "non_consent", "review"),
    ("unconscious", False, "non_consent", "review"),
    ("forced", False, "non_consent", "review"),
    ("non consensual", False, "non_consent", "review"),
    ("nonconsensual", False, "non_consent", "review"),
    ("hidden cam", False, "non_consent", "review"),
    ("hidden camera", False, "non_consent", "review"),
    ("spy cam", False, "non_consent", "review"),
    ("revenge porn", False, "non_consent", "review"),
    ("bestiality", False, "other", "review"),
    ("zoophilia", False, "other", "review"),
]


def seed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    SafetyTerm = apps.get_model("catalog", "SafetyTerm")
    for term, is_regex, category, action in TERMS:
        SafetyTerm.objects.get_or_create(
            term=term, defaults={"is_regex": is_regex, "category": category, "action": action}
        )


def unseed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    SafetyTerm = apps.get_model("catalog", "SafetyTerm")
    SafetyTerm.objects.filter(term__in=[term for term, *_ in TERMS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0004_safetyterm"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
