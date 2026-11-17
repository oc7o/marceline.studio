"""Proof of control of a creator's platform account (ADR 0055): the rules and the workflow.

What these tests hold in place, beyond the state machine:

- **nothing fetches the claimed profile.** `test_no_http_client_is_imported` reads the three modules
  and fails on any import that can open a socket. The address is written by the creator; a preview,
  a crawl or a "just check it resolves" would be a fetch instruction from a stranger;
- **a reissue invalidates the previous code**, because a creator who pasted the old one into their
  bio must not be verified by it later;
- **the notice gate clears through `RightsClaimant.identity_status` and nothing else** — this flow
  adds no second notion of "verified";
- **the event log is append-only in the database**, not by convention.
"""

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from marceline.rights import account_control, services
from marceline.rights import account_control_services as verification
from marceline.rights.gate import Blocker, Work, notice_gate_blockers
from marceline.rights.models import (
    AccountControlChallenge,
    AccountControlEvent,
    AccountControlEventKind,
    IdentityMethod,
    IdentityStatus,
    VerificationMethod,
    VerificationState,
)
from marceline.tests.app_helpers import add_member, make_organisation, make_reference
from marceline.tests.rights_helpers import documents_complete, staff_user

pytestmark = pytest.mark.django_db

RIGHTS = Path(__file__).resolve().parents[1] / "rights"
#: Anything that can open a socket. A module of this flow importing one of these is the failure.
HTTP_IMPORTS = (
    "httpx",
    "requests",
    "urllib.request",
    "urllib3",
    "http.client",
    "socket",
    "aiohttp",
    "playwright",
    "core.http",
)


@pytest.fixture(autouse=True)
def _on(settings: Settings) -> None:
    settings.ACCOUNT_CONTROL_VERIFICATION_ENABLED = True


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


@pytest.fixture
def staff(db: None) -> User:
    return staff_user()


def claim(
    organisation: Organisation,
    user: User,
    *,
    platform: str = "fanhub",
    handle: str = "alpha.creator",
    profile_url: str = "",
) -> AccountControlChallenge:
    return verification.claim_account(
        organisation,
        verification.ClaimInput(platform=platform, handle=handle, profile_url=profile_url),
        services.Actor.customer(user),
    )


# --- the pure rules ------------------------------------------------------------------------------


def test_no_http_client_is_imported() -> None:
    """The claimed profile URL is text a person opens. No module of this flow may be able to fetch
    it — not now, and not after somebody adds "we could just check the page exists"."""
    for name in ("account_control.py", "account_control_services.py", "models.py"):
        source = (RIGHTS / name).read_text()
        for module in HTTP_IMPORTS:
            assert f"import {module}" not in source, f"{name} imports {module}"
            assert f"from {module}" not in source, f"{name} imports from {module}"


def test_a_code_is_prefixed_unambiguous_and_random() -> None:
    codes = {account_control.new_code() for _ in range(200)}

    assert len(codes) == 200, "codes repeat"
    for code in codes:
        assert code.startswith("marceline-")
        assert account_control.looks_like_code(code)
        body = code.removeprefix("marceline-")
        assert len(body) == account_control.CODE_LENGTH
        assert not set(body) & set("01OIL5S8B"), "an ambiguous glyph got into the alphabet"


def test_a_code_lives_seventy_two_hours() -> None:
    now = timezone.now()

    assert account_control.expires_at(now) - now == timedelta(hours=72)


@pytest.mark.parametrize(
    ("raw", "expected"), [("@name", "name"), (" name ", "name"), ("na_me.1-x/", "na_me.1-x")]
)
def test_a_handle_is_normalised(raw: str, expected: str) -> None:
    assert account_control.normalise_handle(raw) == expected


@pytest.mark.parametrize("raw", ["", "a", "https://fanhub.example/name", "na me", "n" * 65])
def test_a_handle_we_will_not_store_is_refused(raw: str) -> None:
    with pytest.raises(account_control.InvalidClaimError):
        account_control.normalise_handle(raw)


@pytest.mark.parametrize(
    "url",
    [
        "https://fanhub.example/alpha",
        "https://www.fanhub.example/alpha",
        "https://fanhub.example./alpha",
    ],
)
def test_the_platforms_own_hosts_are_accepted(url: str) -> None:
    assert account_control.checked_profile_url("fanhub", url).startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "http://fanhub.example/alpha",
        "https://fanhub.example.evil.example/alpha",
        "https://notfanhub.example/alpha",
        "https://user:pass@fanhub.example/alpha",
        "https://fanhub.example:8443/alpha",
        "https://fanhub.example\\@evil.example",
        "https://short.example/abc",
    ],
)
def test_a_link_to_somewhere_else_is_refused(url: str) -> None:
    """Not a security boundary for a request that never happens: a guard against a staff member
    being handed a link to a mirror, a shortener or an unrelated site to open by hand."""
    with pytest.raises(account_control.InvalidClaimError):
        account_control.checked_profile_url("fanhub", url)


def test_a_platform_without_hosts_takes_no_link() -> None:
    assert account_control.checked_profile_url("own_website", "") == ""
    with pytest.raises(account_control.InvalidClaimError):
        account_control.checked_profile_url("own_website", "https://alpha.example/")


# --- claiming and issuing ------------------------------------------------------------------------


def test_a_claim_issues_a_code_and_logs_that_the_creator_saw_it(
    organisation: Organisation, member: User
) -> None:
    challenge = claim(organisation, member, profile_url="https://fanhub.example/alpha.creator")

    assert challenge.state == VerificationState.PENDING
    assert account_control.looks_like_code(challenge.code)
    assert challenge.issue_count == 1
    assert challenge.code_expires_at is not None
    kinds = list(challenge.events.values_list("kind", flat=True))
    assert kinds == [AccountControlEventKind.SHOWN], "the creator asked for it: shown, not issued"


def test_staff_issuing_by_hand_is_logged_as_issued(organisation: Organisation, staff: User) -> None:
    challenge = verification.claim_account(
        organisation,
        verification.ClaimInput(platform="fanbase", handle="alpha"),
        services.Actor.staff(staff),
    )

    assert list(challenge.events.values_list("kind", flat=True)) == [AccountControlEventKind.ISSUED]


def test_a_reissue_invalidates_the_previous_code(
    organisation: Organisation, member: User, staff: User
) -> None:
    challenge = claim(organisation, member)
    first = challenge.code

    reissued = verification.reissue_code(challenge, services.Actor.staff(staff))

    assert reissued.code != first
    assert reissued.issue_count == 2
    assert reissued.state == VerificationState.PENDING
    assert AccountControlChallenge.objects.filter(code=first).count() == 0


def test_claiming_the_same_account_again_replaces_the_code_on_one_row(
    organisation: Organisation, member: User
) -> None:
    first = claim(organisation, member)
    again = claim(organisation, member)

    assert again.pk == first.pk
    assert again.code != first.code
    assert AccountControlChallenge.objects.count() == 1


def test_a_verified_claim_is_not_reopened_by_claiming_it_again(
    organisation: Organisation, member: User, staff: User
) -> None:
    challenge = claim(organisation, member)
    verification.verify(
        challenge, method=VerificationMethod.BIO_CODE, actor=services.Actor.staff(staff)
    )

    again = claim(organisation, member)

    assert again.state == VerificationState.VERIFIED
    assert again.code == "", "a settled claim has no live code"


# --- placing, expiry ------------------------------------------------------------------------------


def test_the_creator_says_it_is_up(organisation: Organisation, member: User) -> None:
    challenge = claim(organisation, member)

    placed = verification.mark_placed(challenge, services.Actor.customer(member))

    assert placed.state == VerificationState.PLACED
    assert placed.placed_by == member
    assert AccountControlEvent.objects.filter(kind=AccountControlEventKind.PLACED).count() == 1


def _age(challenge: AccountControlChallenge, hours: float) -> None:
    """Move one challenge's code into the past without touching anything else."""
    AccountControlChallenge.objects.filter(pk=challenge.pk).update(
        code_expires_at=timezone.now() - timedelta(hours=hours)
    )
    challenge.refresh_from_db()


def test_an_expired_code_reads_as_expired_without_writing_anything(
    organisation: Organisation, member: User
) -> None:
    """A read may not settle a row: on the public host the status comes out of a signed bundle."""
    challenge = claim(organisation, member)
    _age(challenge, 1)

    assert verification.display_state(challenge) == VerificationState.EXPIRED
    challenge.refresh_from_db()
    assert challenge.state == VerificationState.PENDING
    assert challenge.events.count() == 1


def test_an_expired_code_cannot_be_placed_or_verified(
    organisation: Organisation, member: User, staff: User
) -> None:
    challenge = claim(organisation, member)
    _age(challenge, 1)

    with pytest.raises(services.RightsError) as refused:
        verification.mark_placed(challenge, services.Actor.customer(member))

    assert refused.value.code == "code_expired"
    challenge.refresh_from_db()
    assert challenge.state == VerificationState.EXPIRED
    with pytest.raises(services.RightsError) as again:
        verification.verify(
            challenge, method=VerificationMethod.BIO_CODE, actor=services.Actor.staff(staff)
        )
    assert again.value.code == "not_open"


def test_expire_due_settles_open_codes_once(organisation: Organisation, member: User) -> None:
    challenge = claim(organisation, member)
    _age(challenge, 4)

    assert [row.pk for row in verification.expire_due()] == [challenge.pk]
    assert verification.expire_due() == []
    assert challenge.events.filter(kind=AccountControlEventKind.EXPIRED).count() == 1


def test_a_video_call_does_not_need_a_live_code(
    organisation: Organisation, member: User, staff: User
) -> None:
    """The fallback for doubt and for studios establishes nothing through the code, so an expired
    one is beside the point — and only the fact of the check is recorded."""
    challenge = claim(organisation, member)
    _age(challenge, 100)

    verified = verification.verify(
        challenge, method=VerificationMethod.VIDEO_CALL, actor=services.Actor.staff(staff)
    )

    assert verified.state == VerificationState.VERIFIED
    assert verified.method == VerificationMethod.VIDEO_CALL
    claimant = services.claimant_for(organisation)
    assert claimant.identity_method == IdentityMethod.VIDEO_CALL
    assert "video_call" in claimant.identity_evidence_note


# --- where no platform checked anything (operator review 2026-09-19) -----------------------------


def test_a_platform_that_checks_nobody_gets_no_code(
    organisation: Organisation, member: User
) -> None:
    """The creator's own website and "another platform" run no identity check before publication,
    so a code placed there would establish nothing — and none is issued."""
    challenge = claim(organisation, member, platform="own_website", handle="alpha-site")

    assert challenge.code == ""
    assert challenge.code_expires_at is None
    assert challenge.state == VerificationState.PENDING


def test_such_a_claim_cannot_be_placed_or_settled_with_a_code(
    organisation: Organisation, member: User, staff: User
) -> None:
    challenge = claim(organisation, member, platform="other_platform", handle="alpha")

    with pytest.raises(services.RightsError) as placed:
        verification.mark_placed(challenge, services.Actor.customer(member))
    with pytest.raises(services.RightsError) as verified:
        verification.verify(
            challenge, method=VerificationMethod.BIO_CODE, actor=services.Actor.staff(staff)
        )

    assert placed.value.code == verified.value.code == "video_call_required"
    challenge.refresh_from_db()
    assert challenge.state == VerificationState.PENDING


def test_a_video_call_settles_it(organisation: Organisation, member: User, staff: User) -> None:
    challenge = claim(organisation, member, platform="own_website", handle="alpha-site")

    verified = verification.verify(
        challenge, method=VerificationMethod.VIDEO_CALL, actor=services.Actor.staff(staff)
    )

    assert verified.state == VerificationState.VERIFIED
    assert services.claimant_for(organisation).identity_status == IdentityStatus.VERIFIED


def test_the_database_refuses_a_code_method_on_such_a_platform(
    organisation: Organisation, member: User, staff: User
) -> None:
    """Not only the service: a raw write cannot record a bio code as what established identity."""
    challenge = claim(organisation, member, platform="own_website", handle="alpha-site")
    verification.verify(
        challenge, method=VerificationMethod.VIDEO_CALL, actor=services.Actor.staff(staff)
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        AccountControlChallenge.objects.filter(pk=challenge.pk).update(
            method=VerificationMethod.BIO_CODE
        )


def test_every_platform_with_hosts_takes_a_code_and_the_other_two_do_not() -> None:
    """The list and the rule cannot drift apart: a platform we name a host for is one we believe
    checks its creators, and the two without hosts are exactly the video-call ones."""
    assert set(account_control.VIDEO_CALL_ONLY) == {"own_website", "other_platform"}
    for platform in account_control.PLATFORMS:
        assert platform.checks_identity == bool(platform.hosts), platform.code


# --- who may decide -------------------------------------------------------------------------------


def test_only_staff_decide(organisation: Organisation, member: User) -> None:
    challenge = claim(organisation, member)
    customer = services.Actor.customer(member)

    calls: tuple[Callable[[], object], ...] = (
        lambda: verification.verify(challenge, method=VerificationMethod.BIO_CODE, actor=customer),
        lambda: verification.reject(challenge, actor=customer),
        lambda: verification.revoke_verification(challenge, reason="x", actor=customer),
        lambda: verification.reissue_code(challenge, customer),
    )
    for call in calls:
        with pytest.raises(services.RightsError) as refused:
            call()
        assert refused.value.code == "staff_only"


def test_staff_legacy_is_not_a_method_anybody_can_record(
    organisation: Organisation, member: User, staff: User
) -> None:
    challenge = claim(organisation, member)

    with pytest.raises(services.RightsError) as refused:
        verification.verify(
            challenge, method=VerificationMethod.STAFF_LEGACY, actor=services.Actor.staff(staff)
        )

    assert refused.value.code == "unknown_verification_method"
    with pytest.raises(IntegrityError), transaction.atomic():
        AccountControlChallenge.objects.filter(pk=challenge.pk).update(
            method=VerificationMethod.STAFF_LEGACY
        )


def test_the_method_of_an_organisation_verified_outside_this_flow_is_staff_legacy(
    organisation: Organisation, staff: User
) -> None:
    """ADR 0034's admin path is kept. This is how it is attributed, and it is no check of ours."""
    services.verify_identity(
        organisation,
        method=IdentityMethod.COMMERCIAL_REGISTER,
        evidence_note="register court and number seen",
        actor=services.Actor.staff(staff),
    )

    assert verification.method_of(organisation) == VerificationMethod.STAFF_LEGACY


def test_an_unverified_organisation_has_no_method(organisation: Organisation) -> None:
    assert verification.method_of(organisation) == ""


# --- the notice gate ------------------------------------------------------------------------------


def gate_blockers(organisation: Organisation, member: User, staff: User) -> list[str]:
    """One work as far as everything *but* identity can take it, then the gate's verdict."""
    reference = make_reference(member)
    documents_complete(organisation, reference.sha256, staff)
    return notice_gate_blockers(Work(organisation=organisation, sha256=reference.sha256))


def test_verifying_a_claim_clears_identity_not_verified(
    organisation: Organisation, member: User, staff: User
) -> None:
    before = gate_blockers(organisation, member, staff)
    assert Blocker.IDENTITY_NOT_VERIFIED in before

    challenge = claim(organisation, member)
    verification.mark_placed(challenge, services.Actor.customer(member))
    verification.verify(
        challenge,
        method=VerificationMethod.BIO_CODE,
        note="code matched in the public bio",
        actor=services.Actor.staff(staff),
    )

    after = notice_gate_blockers(
        Work(
            organisation=organisation,
            sha256=organisation.owned_originals.get().reference.sha256,
        )
    )
    assert Blocker.IDENTITY_NOT_VERIFIED not in after
    assert services.claimant_for(organisation).identity_status == IdentityStatus.VERIFIED
    assert verification.method_of(organisation) == VerificationMethod.BIO_CODE


def test_a_claim_nobody_decided_clears_nothing(
    organisation: Organisation, member: User, staff: User
) -> None:
    challenge = claim(organisation, member)
    verification.mark_placed(challenge, services.Actor.customer(member))

    assert Blocker.IDENTITY_NOT_VERIFIED in gate_blockers(organisation, member, staff)
    assert challenge.code != ""


def test_a_rejection_leaves_identity_alone(
    organisation: Organisation, member: User, staff: User
) -> None:
    challenge = claim(organisation, member)

    rejected = verification.reject(
        challenge, note="nothing in the bio", actor=services.Actor.staff(staff)
    )

    assert rejected.state == VerificationState.REJECTED
    assert rejected.code == ""
    assert Blocker.IDENTITY_NOT_VERIFIED in gate_blockers(organisation, member, staff)


def test_revoking_the_last_verified_account_blocks_notices_again(
    organisation: Organisation, member: User, staff: User
) -> None:
    challenge = claim(organisation, member)
    verification.verify(
        challenge, method=VerificationMethod.BIO_CODE, actor=services.Actor.staff(staff)
    )
    reference = make_reference(member)
    documents_complete(organisation, reference.sha256, staff)
    work = Work(organisation=organisation, sha256=reference.sha256)
    assert Blocker.IDENTITY_NOT_VERIFIED not in notice_gate_blockers(work)

    verification.revoke_verification(
        challenge, reason="the account changed hands", actor=services.Actor.staff(staff)
    )

    assert Blocker.IDENTITY_NOT_VERIFIED in notice_gate_blockers(work)
    assert services.claimant_for(organisation).identity_status == IdentityStatus.REVOKED


def test_revoking_one_of_two_verified_accounts_keeps_identity(
    organisation: Organisation, member: User, staff: User
) -> None:
    actor = services.Actor.staff(staff)
    first = claim(organisation, member, platform="fanhub", handle="alpha")
    second = claim(organisation, member, platform="fanbase", handle="alpha")
    verification.verify(first, method=VerificationMethod.BIO_CODE, actor=actor)
    verification.verify(second, method=VerificationMethod.DM_CODE, actor=actor)

    verification.revoke_verification(first, reason="handle renamed", actor=actor)

    assert services.claimant_for(organisation).identity_status == IdentityStatus.VERIFIED
    assert verification.method_of(organisation) == VerificationMethod.DM_CODE


def test_a_revocation_needs_a_reason(organisation: Organisation, member: User, staff: User) -> None:
    challenge = claim(organisation, member)
    actor = services.Actor.staff(staff)
    verification.verify(challenge, method=VerificationMethod.BIO_CODE, actor=actor)

    with pytest.raises(services.RightsError) as refused:
        verification.revoke_verification(challenge, reason="   ", actor=actor)

    assert refused.value.code == "reason_required"


# --- the log, and the switch ----------------------------------------------------------------------


def test_the_event_log_is_append_only_in_the_database(
    organisation: Organisation, member: User
) -> None:
    claim(organisation, member)
    event = AccountControlEvent.objects.get()

    with pytest.raises(IntegrityError), transaction.atomic():
        AccountControlEvent.objects.filter(pk=event.pk).update(note="rewritten")

    with pytest.raises(IntegrityError), transaction.atomic():
        event.note = "rewritten"
        event.save(update_fields=["note"])


def test_the_log_never_holds_the_code(organisation: Organisation, member: User) -> None:
    challenge = claim(organisation, member)

    for event in AccountControlEvent.objects.all():
        assert challenge.code not in event.note
        assert challenge.code not in str(event.detail)


def test_the_note_is_never_required(organisation: Organisation, member: User, staff: User) -> None:
    """A required free-text box is where somebody types the thing we promised not to keep."""
    challenge = claim(organisation, member)

    verified = verification.verify(
        challenge, method=VerificationMethod.BIO_CODE, actor=services.Actor.staff(staff)
    )

    assert verified.note == ""


def test_with_the_switch_off_nothing_can_be_claimed_or_verified(
    settings: Settings, organisation: Organisation, member: User, staff: User
) -> None:
    challenge = claim(organisation, member)
    settings.ACCOUNT_CONTROL_VERIFICATION_ENABLED = False

    assert not verification.available()
    calls: tuple[Callable[[], object], ...] = (
        lambda: claim(organisation, member),
        lambda: verification.mark_placed(challenge, services.Actor.customer(member)),
        lambda: verification.verify(
            challenge, method=VerificationMethod.BIO_CODE, actor=services.Actor.staff(staff)
        ),
    )
    for call in calls:
        with pytest.raises(services.RightsError) as refused:
            call()
        assert refused.value.code == "account_verification_unavailable"
