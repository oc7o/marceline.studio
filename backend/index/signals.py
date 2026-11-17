"""Signals from the safety write path to the project layer (docs/safety.md).

The crawler may not import `marceline` (module boundaries), yet two consequences of a stored
safety verdict need it: opening a safety incident with full mirror/cluster deduplication
(`marceline.safety_incidents`) and deciding whether a page credits a customer's rights holder
(`marceline.targets`). The crawler sends `safety_rules_applied` after its transaction commits;
`marceline.apps` connects the receiver (`marceline.safety_rules.after_store`).
"""

from django.dispatch import Signal

#: kwargs: page_ids (list[int]) — pages stored blocked by an incident rule or a
#: customer_release_queue rule, or whose release was revoked.
safety_rules_applied = Signal()
