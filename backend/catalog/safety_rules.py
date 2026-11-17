"""What a safety term's action means, and which version of the rule set decided. Pure, no database.

Operator decisions of 2026-09-17 (docs/safety.md, ADR 0018 amendment) turned the old two actions
(`block`, `review`) into four:

- `review` — ambiguous wording: the page waits in the safety review queue, no media until a human
  clears it.
- `auto_block` — blocked at once, no review step (non-consent wording such as "rape", "forced").
- `auto_block_and_incident` — blocked at once and a safety incident is opened for the operator to
  report by hand (CSAM suspects: "lolita" and the age/minor terms that used to be `block`).
- `customer_release_queue` — blocked at once; only when the page credits a customer's rights
  holder does it go to a separate text-only staff queue, where a human may release it for
  fingerprinting ("flagged term").

Every action except `review` blocks. An action this module does not know (a legacy value, a typo
in a hand-written row) blocks too and counts as incident-worthy: unknown means strictest.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping

REVIEW = "review"
AUTO_BLOCK = "auto_block"
AUTO_BLOCK_AND_INCIDENT = "auto_block_and_incident"
CUSTOMER_RELEASE_QUEUE = "customer_release_queue"
#: The action every former `block` term became (migration catalog 0017).
LEGACY_BLOCK = "block"

ACTIONS = (REVIEW, CUSTOMER_RELEASE_QUEUE, AUTO_BLOCK, AUTO_BLOCK_AND_INCIDENT)
#: Blocking actions that do not open an incident by themselves.
NO_INCIDENT_BLOCK_ACTIONS = frozenset({AUTO_BLOCK, CUSTOMER_RELEASE_QUEUE})

#: Strictness. A human decision made about a term at one action covers that term only while its
#: action is no stricter than it was then (`crawler.safety.resolve`).
_RANK = {REVIEW: 0, CUSTOMER_RELEASE_QUEUE: 1, AUTO_BLOCK: 2, AUTO_BLOCK_AND_INCIDENT: 3}
_UNKNOWN_RANK = max(_RANK.values())


def canonical_action(action: object) -> str:
    """The action as understood today: `block` (before 2026-09-17) is `auto_block_and_incident`."""
    text = str(action or "")
    return AUTO_BLOCK_AND_INCIDENT if text == LEGACY_BLOCK else text


def rank(action: object) -> int:
    return _RANK.get(canonical_action(action), _UNKNOWN_RANK)


def blocks(action: object) -> bool:
    """Every action but `review` blocks; an unknown action blocks as well."""
    return canonical_action(action) != REVIEW


def match_actions(matches: object) -> list[str]:
    """The canonical actions of stored `Page.safety_matches` rows (JSON; malformed rows ignored)."""
    if not isinstance(matches, list):
        return []
    return [canonical_action(m.get("action")) for m in matches if isinstance(m, Mapping)]


def release_queue_only(matches: object) -> bool:
    """True when the page matched something and every match is a `customer_release_queue` term.

    Only such a page can ever be released for fingerprinting: any other hit keeps it blocked.
    """
    actions = match_actions(matches)
    return bool(actions) and all(action == CUSTOMER_RELEASE_QUEUE for action in actions)


def has_release_queue_match(matches: object) -> bool:
    return CUSTOMER_RELEASE_QUEUE in match_actions(matches)


def incident_due(matches: object) -> bool:
    """Whether a *blocked* page with these matches is a case for a hotline report.

    - a term whose action opens incidents matched (or a legacy/unknown blocking action): yes;
    - otherwise, a blocking term explains the block and it opens no incident
      (`auto_block`, `customer_release_queue`): no;
    - otherwise the block is a human's decision (manual block, or review terms only): yes, as
      `safety_incidents` always did.
    """
    if opens_incident(matches):
        return True
    return not any(action in NO_INCIDENT_BLOCK_ACTIONS for action in match_actions(matches))


def opens_incident(matches: object) -> bool:
    """A matched term's own action opens an incident automatically (not a human's block)."""
    return any(
        blocks(action) and action not in NO_INCIDENT_BLOCK_ACTIONS
        for action in match_actions(matches)
    )


def match_keys(matches: object) -> set[tuple[str, str]]:
    """(term, canonical action) of stored matches: what a decision was made about."""
    if not isinstance(matches, list):
        return set()
    return {
        (str(m.get("term", "")), canonical_action(m.get("action")))
        for m in matches
        if isinstance(m, Mapping)
    }


def strictest_action(matches: object) -> str:
    """The strictest canonical action among the matches, or "" when there are none."""
    actions = match_actions(matches)
    return max(actions, key=rank) if actions else ""


def rule_version(rules: Iterable[tuple[str, bool, str, str]]) -> str:
    """A stable version id for a rule set: (term, is_regex, category, action) of every enabled term.

    Content-addressed, so every edit in the admin (or a migration) yields a new version without
    anyone remembering to bump a number, and the same rules always have the same id.
    `catalog.SafetyRuleSet` keeps the snapshot a version stands for.
    """
    rows = sorted(
        {(str(t), bool(r), str(c), canonical_action(a)) for t, r, c, a in rules},
    )
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
    return f"sr-{digest[:12]}"
