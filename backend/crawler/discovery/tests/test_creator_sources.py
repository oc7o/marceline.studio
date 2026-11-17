"""Creator-source discovery templates and their target gate (issue #118).

No search call is made here. The command that supplies the target list lives in the project
package (`marceline/tests/test_discover_creator_sources.py`): `crawler` may not import
`marceline.targets`, which is exactly why `plan_queries` is handed the list instead of finding it.
"""

import pytest

from catalog.models import Creator
from crawler.discovery.services import (
    CREATOR_MARKET,
    CREATOR_SOURCE_TEMPLATES,
    CREATOR_SOURCE_TEMPLATES_DE,
    GERMAN_CREATOR_MARKET,
    plan_queries,
    unsafe_query_reason,
)
from crawler.services import load_safety_terms

pytestmark = pytest.mark.django_db


def test_every_creator_source_template_passes_the_query_safety_refusal() -> None:
    terms = load_safety_terms()
    for template in CREATOR_SOURCE_TEMPLATES + CREATOR_SOURCE_TEMPLATES_DE:
        assert unsafe_query_reason(template.format(name="Example Name"), terms) == "", template


def test_the_creator_markets_refuse_to_plan_without_a_verified_target_list() -> None:
    Creator.objects.create(name="Example Name")
    for market in (CREATOR_MARKET, GERMAN_CREATOR_MARKET):
        with pytest.raises(ValueError, match="only for target creators"):
            plan_queries([], [], market=market)


def test_a_creator_outside_the_target_list_plans_nothing() -> None:
    Creator.objects.create(name="Example Name")
    Creator.objects.create(name="Other Name")
    plan = plan_queries(
        ["Other Name"], [], market=CREATOR_MARKET, target_names=frozenset({"Example Name"})
    )
    assert plan == []


def test_a_target_creator_plans_one_query_per_template_and_alias() -> None:
    Creator.objects.create(name="Example Name", aliases=["Example Alias"])
    plan = plan_queries([], [], market=CREATOR_MARKET, target_names=frozenset({"Example Name"}))
    assert len(plan) == 2 * len(CREATOR_SOURCE_TEMPLATES)
    assert all("Example" in query.text for query in plan)
