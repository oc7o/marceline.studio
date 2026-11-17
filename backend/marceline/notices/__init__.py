"""Notice drafts (issue #33, ADR 0035): customer-sent drafts only.

Marceline detects and documents a technical content match and can prepare a draft notice from
facts it holds plus the customer's own confirmations. It never decides that a use is unlawful,
never chooses a recipient for the customer, and never sends a notice to a real recipient itself —
the customer reviews, signs and sends from their own mailbox or provider account
(docs/legal/answers/01-takedown-notices.md, Part One "Recommended v1 and build-now boundary").

Lives in the project package like `marceline.rights` (ADR 0034): a notice draft joins
`accounts.Organisation`, `matching.ReferenceMatch`/`copy_extent`, `catalog.hosting` and the
customer ownership rules together, a combination no single app may import.
"""
