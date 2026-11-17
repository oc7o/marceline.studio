"""Public REST API (mounted at /api/v1). The OpenAPI schema is committed as backend/openapi.json."""

from django.http import HttpRequest
from ninja import NinjaAPI, Router, Schema

from marceline import originals as original_uploads
from marceline.app import account as app_account
from marceline.app import account_actions as app_account_actions
from marceline.app import cases as app_cases
from marceline.app import data_requests as app_data_requests
from marceline.app import export as app_export
from marceline.app import fingerprints as app_fingerprints
from marceline.app import invites as app_invites
from marceline.app import invoices as app_invoices
from marceline.app import notices as app_notices
from marceline.app import originals as app_originals
from marceline.app import rights_status as app_rights
from marceline.app import session as app_session
from marceline.app import uploads as app_uploads
from marceline.app import verification as app_verification
from marceline.review import (
    claims,
    clusters,
    common_frames,
    creators,
    discovery,
    incidents,
    media,
    originals,
    overview,
    pages,
    references,
    safety,
    safety_release,
)
from marceline.review.auth import router as auth_router
from marceline.review.auth import staff_auth
from marceline.review.schemas import CountsOut
from marceline.safety_rules import queued_count

api = NinjaAPI(title="Marceline API", version="1", urls_namespace="api-v1")


class HealthOut(Schema):
    status: str


@api.get("/health", response=HealthOut, tags=["system"])
def health(request: HttpRequest) -> HealthOut:
    return HealthOut(status="ok")


counts_router = Router(tags=["review"], auth=staff_auth)


@counts_router.get("", response=CountsOut)
def review_counts(request: HttpRequest) -> CountsOut:
    """Open items per queue, for the navigation badges."""
    return CountsOut(
        references=references.proposed_pair_count(),
        clusters=clusters.proposed_cluster_count(),
        safety=safety.review_count(),
        safety_release=queued_count(),
        creators=creators.needs_review_count(),
        claims=claims.open_count(),
        common_frames=common_frames.proposed_common_frame_count(),
    )


api.add_router("/auth", auth_router)
api.add_router("/review/counts", counts_router)
api.add_router("/review/references", references.router)
api.add_router("/review/clusters", clusters.router)
api.add_router("/review/pages", pages.router)
api.add_router("/review/safety", safety.router)
api.add_router("/review/safety/incidents", incidents.router)
api.add_router("/review/safety/customer-release", safety_release.router)
api.add_router("/review/creators", creators.router)
api.add_router("/review/claims", claims.router)
api.add_router("/review/common-frames", common_frames.router)
api.add_router("/media", media.router)
api.add_router("/review/overview", overview.router)
api.add_router("/review/discovery", discovery.router)
api.add_router("/review/originals", originals.router)
# Write side of the originals screen: hashes-only uploads from the browser client (ADR 0019).
api.add_router("/originals", original_uploads.router)

# Customer API (ADR 0021): one organisation's own case. A separate router on purpose — every
# queryset in `marceline/app` is scoped to an organisation and answers 404 for anything else.
# Never widen a review endpoint to serve a customer instead.
api.add_router("/app/auth", app_session.router)
# The mailed account actions (ADR 0054): a second router on the same prefix, because a reset
# and a verification are the same doorway as the sign-in — and because keeping them out of
# `app/session.py` keeps that file about the session and this one about what arrives by mail.
# The first router's routes are session-less by decision; these say `auth=None` per route.
api.add_router("/app/auth", app_account_actions.router)
api.add_router("/app/auth", app_account_actions.signed_in_router)
api.add_router("/app/me", app_account.router)
api.add_router("/app/originals", app_originals.router)
# The case file (docs/customer-ui-plan.md §3): the process, with machine-readable steps only.
api.add_router("/app/findings", app_cases.findings_router)
api.add_router("/app/cases", app_cases.router)
api.add_router("/app/removals", app_cases.removals_router)
# The one customer write besides the session: an original added from fingerprints computed in the
# browser (issue #29). Development only until the production outbox exists (ADR 0028 amendment).
api.add_router("/app/fingerprints", app_fingerprints.router)
# Cloud fingerprinting (issue #59, ADR 0044): the customer sends the file and we hash it. Off
# unless CLOUD_FINGERPRINTING_ENABLED, and development only — production answers 404
# (public_surface.ROLE_ONLY_PREFIXES), because customer media never touches the public host.
api.add_router("/app/uploads", app_uploads.router)
# Billing (ADR 0032): the organisation's issued invoices and their PDFs, read-only.
api.add_router("/app/invoices", app_invoices.router)
# Rights status (ADR 0034): claimant, self-declarations, licensed partners, proof uploads.
# Development only: production answers 404 (public_surface.ROLE_ONLY_PREFIXES).
api.add_router("/app/rights", app_rights.router)
# Notice drafts (issue #33, ADR 0035): customer-sent drafts only, nothing sent to a real
# recipient. Development only: production answers 404 (public_surface.ROLE_ONLY_PREFIXES).
api.add_router("/app/notices", app_notices.router)
# The customer's own data export (ADR 0045 amendment 2026-09-18, issue #85): the same document
# `manage.py export_customer_data` writes, as a download. Development only: production holds no
# originals, rights records or notice drafts beyond its signed cache bundle
# (public_surface.ROLE_ONLY_PREFIXES).
api.add_router("/app/export", app_export.router)
# Onboarding by invite (issue #115, ADR 0052): a customer surface reachable **without a session**,
# which is why every failure there answers one identical 404. Off unless ONBOARDING_INVITES_ENABLED.
# Public since slice 2 (2026-09-19): on production the last step creates nothing and hands the
# redemption to the outbox instead (ADR 0052 §9).
api.add_router("/app/invite", app_invites.router)
# Proof of control of the creator's own platform account (ADR 0055): the code they place in their
# own profile and a staff member checks by hand. Off unless ACCOUNT_CONTROL_VERIFICATION_ENABLED,
# in which case it reaches production like rights and notices — reads from the signed bundle, the
# two writes through the outbox.
api.add_router("/app/verification", app_verification.router)
# The creator's own data requests (ADR 0056, issue #133): an export of their data and a deletion of
# it, both asked for from inside the app. Off unless CUSTOMER_DATA_REQUESTS_ENABLED, and reaching
# production like rights and notices — the read from the signed bundle, both writes through the
# outbox — because the creator this exists for may only ever see the public host.
api.add_router("/app/data-requests", app_data_requests.router)
