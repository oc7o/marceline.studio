"""Regression tests for `catalog/safety_term_defaults.py` and the `_ensure_safety_terms` fixture
in `backend/conftest.py`.

The bug: the 34 starter `SafetyTerm` rows are seeded once, by data migrations, when the test
database is built. A `django_db(transaction=True)` test truncates every table in its teardown
(`django.test.TransactionTestCase._fixture_teardown`, which calls the `flush` management command
unconditionally) — the migrations never run again, so without a per-test fix those rows are gone
for every later test in the same worker. Whether that later test still sees them used to depend on
test order, worker assignment and xdist's `--dist` mode.
"""

import pytest
from django.core.management import call_command

from catalog.models import SafetyTerm
from catalog.safety_term_defaults import DEFAULT_SAFETY_TERMS, ensure_default_safety_terms


@pytest.mark.django_db
def test_ensure_default_safety_terms_restores_rows_a_transactional_flush_wiped() -> None:
    assert SafetyTerm.objects.count() == 34  # seeded already, by the conftest fixture

    # Exactly what a `django_db(transaction=True)` test's teardown does to every table, real
    # bug reproduced directly rather than relying on some other test happening to run first.
    call_command("flush", verbosity=0, interactive=False, reset_sequences=False)
    assert SafetyTerm.objects.count() == 0

    ensure_default_safety_terms()
    assert SafetyTerm.objects.count() == len(DEFAULT_SAFETY_TERMS) == 34
    assert SafetyTerm.objects.get(term="jailbait").action == "auto_block_and_incident"
    assert SafetyTerm.objects.get(term="flagged term").action == "customer_release_queue"


@pytest.mark.django_db
def test_ensure_default_safety_terms_never_touches_an_existing_row() -> None:
    SafetyTerm.objects.filter(term="jailbait").update(enabled=False)
    ensure_default_safety_terms()
    assert SafetyTerm.objects.get(term="jailbait").enabled is False


# Both tests below are `transaction=True`, so pytest-django's own test-ordering hook (it runs
# transactional tests as a block, after every non-transactional `django_db` test — see
# `pytest_collection_modifyitems` in pytest_django/plugin.py) keeps their declared order stable
# instead of separating them; `xdist_group` pins them to the same xdist worker so `-n auto` (any
# `--dist` mode) cannot split them onto separate, separately-seeded databases either. Together
# that makes this a deterministic reproduction of the real integration bug: without the
# `_ensure_safety_terms` autouse fixture, the second test fails after the first one's teardown
# truncates `catalog.SafetyTerm`, exactly as the suite did before this fix (22-39 failures under
# `pytest -n 8 --dist loadfile`, passing under `--dist load` only by luck of the draw).
@pytest.mark.xdist_group(name="safety_term_reseed_regression")
@pytest.mark.django_db(transaction=True)
def test_a_a_transactional_test_truncates_every_table() -> None:
    assert SafetyTerm.objects.count() == 34


@pytest.mark.xdist_group(name="safety_term_reseed_regression")
@pytest.mark.django_db(transaction=True)
def test_b_safety_terms_survive_a_preceding_transactional_test() -> None:
    assert SafetyTerm.objects.count() == 34
