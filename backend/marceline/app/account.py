"""`GET /api/v1/app/me`: the signed-in account and the organisation its requests are scoped to.

An account with no organisation is answered with `organisation: null` and a 200. It is a real
account that owns nothing — a staff reviewer, or a customer whose organisation has not been set up
yet — and saying so leaks nothing about anybody else's data.
"""

from django.contrib.auth.models import User
from django.http import HttpRequest
from django.views.decorators.csrf import ensure_csrf_cookie
from ninja import Router
from ninja.decorators import decorate_view

from accounts import account_actions, action_tokens
from marceline.app.auth import RequestUser, customer_auth
from marceline.app.coverage import monitored_sources
from marceline.app.export import export_available
from marceline.app.fingerprints import adding_originals_available
from marceline.app.ownership import membership_of
from marceline.app.schemas import AccountOut, OrganisationOut
from marceline.app.uploads import uploads_available
from marceline.deletion import requests as data_requests
from marceline.rights import account_control_services as verification

router = Router(tags=["app"], auth=customer_auth)


def account_out(user: RequestUser) -> AccountOut:
    """The account and its scope. Also the answer to a successful login (`app/session.py`)."""
    membership = membership_of(user)
    return AccountOut(
        username=user.get_username(),
        organisation=None
        if membership is None
        else OrganisationOut(
            id=membership.organisation.public_id,
            name=membership.organisation.name,
            segment=membership.organisation.segment,  # type: ignore[arg-type]
            role=membership.role,  # type: ignore[arg-type]
            created_at=membership.organisation.created_at,
        ),
        monitored_sources=monitored_sources(),
        can_add_originals=membership is not None and adding_originals_available(),
        # Cloud fingerprinting (issue #59, ADR 0044). False unless this machine really takes files:
        # the switch is off almost everywhere, and it is never true on production.
        can_upload_media=membership is not None
        and adding_originals_available()
        and uploads_available(),
        can_export_data=membership is not None and export_available(),
        # ADR 0055: proof of control of the creator's own platform account. Unlike the upload
        # routes this one may run on the public host (bundle read, outbox writes), so the only
        # question is whether the switch is on where the request is answered.
        can_verify_account=membership is not None and verification.available(),
        # ADR 0056: the two data requests. Like verification and unlike the export download, this
        # one may run on the public host, so the only question is whether the switch is on where
        # the request is answered.
        can_request_data=membership is not None and data_requests.available(),
        # The address verification (ADR 0054). A fact, never a gate: nothing in the beta refuses a
        # signed-in customer over an unopened mail, and the app renders a banner that asks.
        email_verified=account_actions.verified(user) if isinstance(user, User) else False,
        can_verify_email=membership is not None and action_tokens.available(),
    )


@router.get("", response=AccountOut)
@decorate_view(ensure_csrf_cookie)
def me(request: HttpRequest) -> AccountOut:
    """Who is signed in, and which organisation every other customer request is scoped to.

    Also refreshes the CSRF cookie, so a session restored from a cookie can post again without
    calling `GET /api/v1/app/auth/session` first.
    """
    return account_out(request.user)
