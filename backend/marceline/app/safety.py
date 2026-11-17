"""What a customer may be shown of a page found on a piracy site. Three rules, all negative.

**A safety-flagged page is never exposed, in any form** (ADR 0011, ADR 0018,
docs/customer-ui-plan.md §4): not listed, not counted, not summarised as "one hidden result". The
media gate already guarantees such a page has no fetchable media; this filter guarantees the page
itself never reaches a customer response. A page that merely matched a safety term is excluded too,
even if a reviewer later cleared it — the same conservative rule the capture triage uses, because
there is no customer-facing reason to relitigate a term hit.

**A page the media gate does not allow is not exposed either** (ADR 0018). The gate is
`index.services.page_media_allowed`: clear *and* scored at least `SAFETY_GATE_MIN_SCORE`. A page
whose own text scored below it — a near miss, thin text, a risky site — is not clearly safe enough
to fetch a picture from, so it is not clearly safe enough to put in front of a customer either.
**A page that was never scored is not visible**: nothing has judged its text yet. The rule is not
copied here: `page_visible` calls the gate, and `visible_page_filter` states the same condition as
a query over the same `gate_min_score()` threshold; `test_app_originals.py` asserts that the two
agree on every status, score and term-hit combination.

**No pirate imagery at all in phase 1** (issue #53). This is enforced by
absence: no schema in `marceline/app` has a media field, and nothing here builds a media
reference. Evidence is site, URL and dates — text a rights holder can verify by clicking.
"""

from django.db.models import Q

from index.models import Page
from index.services import gate_min_score, page_media_allowed


def visible_page_filter(prefix: str = "") -> Q:
    """Q over pages a customer may see; `prefix` names the relation, e.g. "page" on a leak row.

    `page_media_allowed` as a query — clear, and scored at or above the gate (a NULL score never
    satisfies `gte`) — plus no safety-term hit, ever.
    """
    field = f"{prefix}__" if prefix else ""
    return Q(
        **{
            f"{field}safety_status": Page.SafetyStatus.CLEAR,
            f"{field}safety_score__gte": gate_min_score(),
            f"{field}safety_matches": [],
        }
    )


def hidden_page_filter(prefix: str = "") -> Q:
    """Q over pages a customer may **not** see: the negation of `visible_page_filter`.

    Spelled out rather than written `~visible_page_filter()`, because `safety_score` is nullable
    and SQL's three-valued logic makes `NOT (status = 'clear' AND score >= x AND matches = '[]')`
    evaluate to NULL — not true — for a page that was never scored. A negation that silently drops
    the never-scored pages would be the wrong way round for a filter whose job is to find what
    must be hidden (`marceline/cache_purge.py`).
    """
    field = f"{prefix}__" if prefix else ""
    return (
        ~Q(**{f"{field}safety_status": Page.SafetyStatus.CLEAR})
        | Q(**{f"{field}safety_score__isnull": True})
        | Q(**{f"{field}safety_score__lt": gate_min_score()})
        | ~Q(**{f"{field}safety_matches": []})
    )


def fields_visible(
    safety_status: str | None, safety_score: float | None, safety_matches: object
) -> bool:
    """The same rule over the three stored fields alone, for callers that hold no `Page`."""
    return page_media_allowed(safety_status, safety_score) and not safety_matches


def page_visible(page: Page) -> bool:
    """The same rule for a page already in memory."""
    return fields_visible(page.safety_status, page.safety_score, page.safety_matches)
