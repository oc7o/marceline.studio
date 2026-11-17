"""The customer API (`/api/v1/app/…`): what a paying creator or studio sees of their own case.

Deliberately separate from `marceline/review` (the staff moderation API), not a widened version of
it. The two audiences ask different questions — a reviewer decides, a customer is told — and every
queryset here is scoped to one organisation. Sharing a router with the staff API is how tenant
leaks happen (docs/customer-ui-plan.md §5).

Three rules hold everywhere in this package:
- **Scope through `ownership`.** No view builds its own `Reference` queryset.
- **404, never 403.** A resource another organisation owns is indistinguishable from one that does
  not exist; a 403 would confirm it exists.
- **No pirate imagery, no flagged pages** (`safety`). Phase 1 ships evidence as text.
"""
