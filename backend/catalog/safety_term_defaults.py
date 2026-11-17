"""The starter `SafetyTerm` rows, as the combined effect of migrations `0005_seed_safety_terms`
and `0017_safety_rule_actions` leaves them (docs/safety.md). Production never calls
`ensure_default_safety_terms`: these 34 rows live in the database from the migrations and the
admin from then on, and a real deployment never resets them.

Test support only. A `django_db(transaction=True)` test flushes every table, including
`SafetyTerm`, in its teardown (`django.test.TransactionTestCase._fixture_teardown`) — a data
migration only runs once, when the test database is built, so those rows do not come back on
their own. `backend/conftest.py`'s `_ensure_safety_terms` fixture calls this before every test
that touches the database, restoring rows a preceding transactional test wiped, regardless of
test order, worker or `--dist` mode. `get_or_create` never touches a row a test deliberately
changed (e.g. disabling one) — it only replaces what is missing.
"""

from __future__ import annotations

# (term, is_regex, category, action) — the state after both migrations, recomputed from them
# in the session that added this file rather than retyped by hand.
DEFAULT_SAFETY_TERMS: tuple[tuple[str, bool, str, str], ...] = (
    ("underage", False, "minor", "auto_block_and_incident"),
    ("under age", False, "minor", "auto_block_and_incident"),
    ("preteen", False, "minor", "auto_block_and_incident"),
    ("pre teen", False, "minor", "auto_block_and_incident"),
    ("child porn", False, "minor", "auto_block_and_incident"),
    ("childporn", False, "minor", "auto_block_and_incident"),
    ("cp video", False, "minor", "auto_block_and_incident"),
    ("pthc", False, "minor", "auto_block_and_incident"),
    ("jailbait", False, "minor", "auto_block_and_incident"),
    ("loli", False, "minor", "auto_block_and_incident"),
    ("shota", False, "minor", "auto_block_and_incident"),
    (
        r"(?:[1-9]|1[0-7]) ?(?:y ?o|yrs? old|years? old)",
        True,
        "minor",
        "auto_block_and_incident",
    ),
    ("lolita", False, "minor", "auto_block_and_incident"),
    ("lolitas", False, "minor", "auto_block_and_incident"),
    ("flagged term", False, "minor", "customer_release_queue"),
    ("rape", False, "non_consent", "auto_block"),
    ("raped", False, "non_consent", "auto_block"),
    ("rapes", False, "non_consent", "auto_block"),
    ("raping", False, "non_consent", "auto_block"),
    ("forced", False, "non_consent", "auto_block"),
    ("drugged", False, "non_consent", "review"),
    ("passed out", False, "non_consent", "review"),
    ("unconscious", False, "non_consent", "review"),
    ("non consensual", False, "non_consent", "review"),
    ("nonconsensual", False, "non_consent", "review"),
    ("hidden cam", False, "non_consent", "review"),
    ("hidden camera", False, "non_consent", "review"),
    ("spy cam", False, "non_consent", "review"),
    ("revenge porn", False, "non_consent", "review"),
    ("bestiality", False, "other", "review"),
    ("zoophilia", False, "other", "review"),
)


def ensure_default_safety_terms() -> None:
    """Insert any of `DEFAULT_SAFETY_TERMS` missing from the database. Idempotent, cheap when
    nothing is missing (one query), and never overwrites a row that already exists."""
    from catalog.models import SafetyTerm

    existing = set(
        SafetyTerm.objects.filter(term__in=[term for term, *_ in DEFAULT_SAFETY_TERMS]).values_list(
            "term", flat=True
        )
    )
    for term, is_regex, category, action in DEFAULT_SAFETY_TERMS:
        if term in existing:
            continue
        SafetyTerm.objects.get_or_create(
            term=term, defaults={"is_regex": is_regex, "category": category, "action": action}
        )
