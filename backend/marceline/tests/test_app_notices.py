"""Customer notice-draft API (issue #33, ADR 0035): scoped, codes only, 404 byte-identical,
development-only."""

from collections.abc import Callable
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from accounts.models import Organisation
from catalog.models import (
    ComplaintChannel,
    DirectoryProvider,
    HosterContact,
    ProviderIdentifier,
)
from index.models import Page
from marceline.hosting import observe_notice_removals
from marceline.notices import declarations
from marceline.notices.models import (
    REQUIRED_CONFIRMATIONS,
    ConfirmationKind,
    NoticeDraft,
    NoticeEvent,
    NoticeEventKind,
)
from marceline.notices.services import unknown_notice_public_id
from marceline.public_surface import role_allows
from marceline.tests.app_helpers import (
    APP,
    UNKNOWN_ORIGINAL,
    add_member,
    client_for,
    make_organisation,
    make_reference,
    public_id_of,
)
from marceline.tests.notices_helpers import make_hosting_record, playable, rights_ready
from marceline.tests.test_app_original_groups import twin
from marceline.tests.test_app_originals import match
from matching.fingerprint import PIPELINE_V0
from matching.models import Reference

NOTICES = f"{APP}/notices"
#: A well-formed notice id no draft ever has: the "does not exist" side of a 404 comparison.
UNKNOWN_NOTICE = unknown_notice_public_id()


@pytest.fixture
def organisation(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def member(organisation: Organisation) -> User:
    return add_member(organisation, "alpha-owner")


@pytest.fixture
def staff(db: None) -> User:
    return User.objects.create_user("reviewer", password="secret-pass-123", is_staff=True)


@pytest.fixture
def client(member: User) -> Client:
    return client_for(member)


@pytest.fixture
def ready_original(
    organisation: Organisation, member: User, staff: User, make_page: Callable[[str], Page]
) -> tuple[Reference, Page]:
    reference = make_reference(member)
    page = playable(make_page("candidate"))
    make_hosting_record(page.site.domain)
    match(
        reference,
        page,
        band="high",
        status="confirmed",
        copy_extent="full_copy",
        copy_extent_version=1,
    )
    rights_ready(organisation, reference.sha256, member, staff)
    return reference, page


def _confirmations() -> list[dict[str, object]]:
    return [
        {
            "kind": kind,
            "version": declarations.current(kind).version,
            "language": "en",
            "confirmed": True,
        }
        for kind in REQUIRED_CONFIRMATIONS
    ]


def _create(client: Client, original_id: str, page_id: str, hostname: str) -> dict[str, object]:
    response = client.post(
        NOTICES,
        {
            "original_id": original_id,
            "page_ids": [page_id],
            "recipient": {
                "hostname": hostname,
                "provider_name": "Example Hosting",
                "channel_kind": "abuse_email",
                "email": "abuse@hosting.example",
            },
            "variant": "dsa_art16",
            "language": "en",
            "claimant": {"name": "Alpha Studio", "email": "rights@alpha.example"},
        },
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return cast(dict[str, Any], response.json())


def test_candidates_lists_the_confirmed_finding_with_its_blockers(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    reference, page = ready_original

    response = client.get(f"{NOTICES}/candidates/{public_id_of(reference)}")

    assert response.status_code == 200
    body = response.json()
    [finding] = body["findings"]
    assert finding["page_id"] == page.public_id
    assert finding["blockers"] == []
    assert finding["providers"]


def test_a_copy_confirmed_on_another_reference_of_the_file_is_eligible(
    organisation: Organisation,
    member: User,
    staff: User,
    client: Client,
    make_page: Callable[[str], Page],
) -> None:
    """A re-fingerprint is another `Reference` of the same file (ADR 0022, issue #70), and the
    human confirmation stays on the row that was reviewed. The customer is shown one confirmed
    copy either way (`confirmed_findings_of`), so the notice gate must see the same confirmation
    — reading only the representative's rows refused a notice for a copy the customer had
    confirmed, and left `create` and `approve` disagreeing about it.
    """
    reviewed = make_reference(member, title="The video (v0)")
    Reference.objects.filter(pk=reviewed.pk).update(pipeline=PIPELINE_V0)
    representative = twin(reviewed, organisation)
    page = playable(make_page("candidate"))
    make_hosting_record(page.site.domain)
    match(reviewed, page, status="confirmed", copy_extent="full_copy", copy_extent_version=1)
    # The newer grid re-proposed the same copy; nobody has decided that row.
    match(representative, page, status="proposed", copy_extent="full_copy", copy_extent_version=1)
    rights_ready(organisation, reviewed.sha256, member, staff)
    original_id = public_id_of(representative)

    [finding] = client.get(f"{NOTICES}/candidates/{original_id}").json()["findings"]

    assert finding["blockers"] == []
    assert finding["requires_exception_review"] is False
    draft = _create(client, original_id, page.public_id, page.site.domain)
    approved = client.post(
        f"{NOTICES}/{draft['id']}/approve", _confirmations(), content_type="application/json"
    )
    assert approved.status_code == 200, approved.content


def test_a_rejection_on_any_reference_of_the_file_vetoes_the_confirmation_on_another(
    organisation: Organisation,
    member: User,
    staff: User,
    client: Client,
    make_page: Callable[[str], Page],
) -> None:
    """The other half of reading the whole file: a human's **no** is not overruled by a grid.

    `no_high_confirmed_match` is this gate's only human review step. If any reference of the file
    carries a rejected match for this page, a confirmed row on another one may not stand in for
    it — not in `candidates`, not in `create_draft`, and not in `approve`, which all read
    `gate.file_matches`.
    """
    reviewed = make_reference(member, title="The video (v0)")
    Reference.objects.filter(pk=reviewed.pk).update(pipeline=PIPELINE_V0)
    representative = twin(reviewed, organisation)
    page = playable(make_page("candidate"))
    make_hosting_record(page.site.domain)
    # Confirmed on the old grid, rejected by a reviewer on the current one.
    match(reviewed, page, status="confirmed", copy_extent="full_copy", copy_extent_version=1)
    match(representative, page, status="rejected", copy_extent="full_copy", copy_extent_version=1)
    rights_ready(organisation, reviewed.sha256, member, staff)
    original_id = public_id_of(representative)

    [finding] = client.get(f"{NOTICES}/candidates/{original_id}").json()["findings"]

    assert finding["blockers"] == ["no_high_confirmed_match"]
    refused = _create_with(client, _draft_body(original_id, page.public_id, page.site.domain))
    assert refused.status_code == 409
    assert refused.json() == {"detail": "finding_not_eligible"}


def test_a_rejection_after_the_draft_was_created_refuses_the_approval(
    organisation: Organisation,
    member: User,
    staff: User,
    client: Client,
    make_page: Callable[[str], Page],
) -> None:
    """The veto is re-checked at approval too: a reviewer can say no after the draft exists,
    on any reference of the file, and the frozen text must never be produced."""
    reviewed = make_reference(member, title="The video (v0)")
    Reference.objects.filter(pk=reviewed.pk).update(pipeline=PIPELINE_V0)
    representative = twin(reviewed, organisation)
    page = playable(make_page("candidate"))
    make_hosting_record(page.site.domain)
    confirmed = match(
        reviewed, page, status="confirmed", copy_extent="full_copy", copy_extent_version=1
    )
    rights_ready(organisation, reviewed.sha256, member, staff)
    original_id = public_id_of(representative)
    draft = _create(client, original_id, page.public_id, page.site.domain)

    # A reviewer rejects the copy on the newer grid after the draft was prepared.
    match(representative, page, status="rejected", copy_extent="full_copy", copy_extent_version=1)

    approved = client.post(
        f"{NOTICES}/{draft['id']}/approve", _confirmations(), content_type="application/json"
    )

    assert approved.status_code == 409
    assert approved.json() == {"detail": "finding_no_longer_eligible"}
    assert confirmed.status == "confirmed"  # the other row is untouched; only the gate says no


def test_candidates_404_for_an_unowned_original(client: Client) -> None:
    response = client.get(f"{NOTICES}/candidates/{UNKNOWN_ORIGINAL}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Original not found"}


def test_create_approve_and_mark_sent_end_to_end(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    reference, page = ready_original

    draft = _create(client, public_id_of(reference), page.public_id, page.site.domain)
    assert draft["status"] == "draft"
    assert draft["preview_text"]
    assert draft["approved_text"] is None
    assert draft["recipient_email"] == "abuse@hosting.example"

    approved = client.post(
        f"{NOTICES}/{draft['id']}/approve", _confirmations(), content_type="application/json"
    )
    assert approved.status_code == 200, approved.content
    approved_body = approved.json()
    assert approved_body["status"] == "customer_approved"
    assert approved_body["approved_text"]
    assert approved_body["preview_text"] is None

    sent = client.post(f"{NOTICES}/{draft['id']}/mark-sent")
    assert sent.status_code == 200
    assert sent.json()["status"] == "marked_sent_by_customer"
    # Nothing has been observed yet: the page is still up, and the field says exactly that.
    assert sent.json()["removal_observed_at"] is None

    responded = client.post(
        f"{NOTICES}/{draft['id']}/response",
        {"outcome": "removed"},
        content_type="application/json",
    )
    assert responded.status_code == 200
    assert responded.json()["outcome"] == "removed"

    # The stamp comes from an availability check, never from what the customer typed above.
    page.availability = Page.Availability.REMOVED
    page.availability_checked_at = timezone.now()
    page.save(update_fields=["availability", "availability_checked_at"])
    assert observe_notice_removals() == 1
    assert client.get(f"{NOTICES}/{draft['id']}").json()["removal_observed_at"] is not None


def _approved(client: Client, ready: tuple[Reference, Page]) -> dict[str, Any]:
    reference, page = ready
    draft = _create(client, public_id_of(reference), page.public_id, page.site.domain)
    approved = client.post(
        f"{NOTICES}/{draft['id']}/approve", _confirmations(), content_type="application/json"
    )
    assert approved.status_code == 200, approved.content
    return cast(dict[str, Any], approved.json())


def test_export_is_refused_until_the_customer_has_approved_the_draft(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    """The export may never be a way past the confirmations: approval comes before export."""
    reference, page = ready_original
    draft = _create(client, public_id_of(reference), page.public_id, page.site.domain)

    response = client.get(f"{NOTICES}/{draft['id']}/export/txt")

    assert response.status_code == 409
    assert response.json() == {"detail": "not_approved"}
    assert not NoticeEvent.objects.filter(kind=NoticeEventKind.EXPORTED).exists()


def test_export_hands_over_the_three_files_and_logs_each_download(
    client: Client, ready_original: tuple[Reference, Page], mailoutbox: list[object]
) -> None:
    _reference, page = ready_original
    notice = _approved(client, ready_original)

    text = client.get(f"{NOTICES}/{notice['id']}/export/txt")
    eml = client.get(f"{NOTICES}/{notice['id']}/export/eml")
    annex = client.get(f"{NOTICES}/{notice['id']}/export/annex")

    # The text file is the approved text byte for byte — the sha256 the record names.
    assert text.status_code == 200
    assert text["Content-Type"] == "text/plain; charset=utf-8"
    assert text.content.decode() == notice["approved_text"]
    assert text["Content-Disposition"] == f'attachment; filename="{notice["id"]}.txt"'
    assert text["Cache-Control"] == "private, no-store"

    # The .eml is an unsent message addressed to the adopted channel, with no sender of ours.
    assert eml.status_code == 200
    assert eml["Content-Type"] == "message/rfc822"
    body = eml.content.decode()
    assert "To: abuse@hosting.example" in body
    assert "X-Unsent: 1" in body
    assert "From:" not in body

    assert annex.status_code == 200
    assert page.url in annex.content.decode()

    # Nothing left this machine by mail: an export is bytes for the browser.
    assert mailoutbox == []

    events = list(
        NoticeEvent.objects.filter(kind=NoticeEventKind.EXPORTED).order_by("pk").values("detail")
    )
    assert [row["detail"]["format"] for row in events] == ["txt", "eml", "annex"]
    assert {row["detail"]["sha256"] for row in events} == {
        NoticeDraft.objects.get(public_id=notice["id"]).approved_text_sha256
    }
    # The log names the text, it does not keep a second copy of it.
    assert all("text" not in row["detail"] for row in events)


def test_an_unknown_export_format_is_the_same_404_as_an_unknown_notice(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    notice = _approved(client, ready_original)

    unknown_format = client.get(f"{NOTICES}/{notice['id']}/export/pdf")
    unknown_notice = client.get(f"{NOTICES}/{UNKNOWN_NOTICE}/export/txt")

    assert unknown_format.status_code == 404
    assert unknown_format.content == unknown_notice.content


def test_a_withdrawn_draft_is_not_a_notice_to_take_away(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    notice = _approved(client, ready_original)
    withdrawn = client.post(
        f"{NOTICES}/{notice['id']}/withdraw",
        {"reason": "customer_declined"},
        content_type="application/json",
    )
    assert withdrawn.status_code == 200

    response = client.get(f"{NOTICES}/{notice['id']}/export/txt")

    assert response.status_code == 409
    assert response.json() == {"detail": "already_closed"}


def test_a_safety_flagged_page_stops_the_export_of_its_draft(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    """A page flagged after approval hides the whole draft — including its files (ADR 0011/0018)."""
    _reference, page = ready_original
    notice = _approved(client, ready_original)
    page.safety_matches = ["a term"]
    page.save(update_fields=["safety_matches"])

    response = client.get(f"{NOTICES}/{notice['id']}/export/txt")

    assert response.status_code == 404
    assert page.url not in response.content.decode()


def test_another_organisation_cannot_download_a_draft(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    notice = _approved(client, ready_original)
    other = client_for(add_member(make_organisation("Other Studio"), "other-downloader"))

    response = other.get(f"{NOTICES}/{notice['id']}/export/txt")

    assert response.status_code == 404
    assert response.json() == {"detail": "Notice not found"}


def test_create_refuses_an_ineligible_finding_with_404_for_an_unlisted_page(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    reference, _page = ready_original

    response = client.post(
        NOTICES,
        {
            "original_id": public_id_of(reference),
            "page_ids": ["page_" + "0" * 32],
            "recipient": {"hostname": "hoster.example", "channel_kind": "abuse_email"},
            "variant": "dsa_art16",
            "language": "en",
            "claimant": {"name": "Alpha Studio", "email": "rights@alpha.example"},
        },
        content_type="application/json",
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Finding not found"}


def test_a_notice_is_invisible_to_another_organisation(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    reference, page = ready_original
    draft = _create(client, public_id_of(reference), page.public_id, page.site.domain)
    other = client_for(add_member(make_organisation("Other Studio"), "other-owner"))

    response = other.get(f"{NOTICES}/{draft['id']}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Notice not found"}


def test_a_malformed_notice_id_gets_the_same_404(client: Client) -> None:
    response = client.get(f"{NOTICES}/not-a-real-id")

    assert response.status_code == 404
    assert response.json() == {"detail": "Notice not found"}


def test_without_organisation_or_session(db: None) -> None:
    staff = User.objects.create_user("staff", password="secret-pass-123", is_staff=True)

    assert Client().get(NOTICES).status_code == 401
    assert client_for(staff).get(NOTICES).status_code == 200
    assert client_for(staff).get(NOTICES).json() == []


def test_sandbox_send_is_refused_for_a_non_sandbox_organisation(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    reference, page = ready_original
    draft = _create(client, public_id_of(reference), page.public_id, page.site.domain)
    client.post(
        f"{NOTICES}/{draft['id']}/approve", _confirmations(), content_type="application/json"
    )

    response = client.post(
        f"{NOTICES}/sandbox-send/{draft['id']}",
        {"recipient_email": "sandbox@marceline.test"},
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "not_sandbox"}


@pytest.mark.parametrize("role", ["dev", "prod"])
def test_notices_reach_production_except_the_sandbox_send(role: str) -> None:
    """ADR 0046: the drafting flow is served on the public host; the one path that may hand a
    draft to a mail transport is not, and its prefix is what refuses it there."""
    for path in (
        NOTICES,
        f"{NOTICES}/candidates/{UNKNOWN_ORIGINAL}",
        f"{NOTICES}/notice_x/approve",
        f"{NOTICES}/notice_x/export/txt",
        f"{NOTICES}/submissions",
    ):
        assert role_allows(role, path) is True

    assert role_allows(role, f"{NOTICES}/sandbox-send/notice_x") is (role == "dev")


def _create_with(client: Client, body: dict[str, Any]) -> Any:
    return client.post(NOTICES, body, content_type="application/json")


def _draft_body(original_id: str, page_id: str, hostname: str, **recipient: Any) -> dict[str, Any]:
    choice: dict[str, Any] = {
        "hostname": hostname,
        "provider_name": "Example Hosting",
        "channel_kind": "abuse_email",
        "email": "abuse@hosting.example",
    }
    choice.update(recipient)
    return {
        "original_id": original_id,
        "page_ids": [page_id],
        "recipient": choice,
        "variant": "dsa_art16",
        "language": "en",
        "claimant": {"name": "Alpha Studio", "email": "rights@alpha.example"},
    }


def test_a_draft_stops_being_served_once_its_page_is_safety_flagged(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    """ADR 0011 / ADR 0018: a customer response never mentions a safety-flagged page.

    A page can be flagged *after* a draft was prepared (a re-crawl, a changed term list,
    `safety_scan`). The case file drops such a finding at once; a notice draft carries the same
    URL in its own snapshot and must disappear with it — detail, list and every action on it.
    """
    reference, page = ready_original
    draft = _create(client, public_id_of(reference), page.public_id, page.site.domain)
    page.safety_status = Page.SafetyStatus.BLOCKED
    page.safety_matches = ["a term"]
    page.save(update_fields=["safety_status", "safety_matches"])

    detail = client.get(f"{NOTICES}/{draft['id']}")
    missing = client.get(f"{NOTICES}/{UNKNOWN_NOTICE}")
    listed = client.get(NOTICES)
    approved = client.post(
        f"{NOTICES}/{draft['id']}/approve", _confirmations(), content_type="application/json"
    )

    assert page.url not in detail.content.decode()
    assert (detail.status_code, detail.content) == (404, missing.content)
    assert listed.json() == []
    assert page.url not in listed.content.decode()
    assert approved.status_code == 404


def test_a_draft_stops_being_served_once_its_page_only_matches_a_safety_term(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    """The customer filter is stricter than "blocked": one term hit hides a page for good."""
    reference, page = ready_original
    draft = _create(client, public_id_of(reference), page.public_id, page.site.domain)
    page.safety_matches = ["a term"]
    page.save(update_fields=["safety_matches"])

    detail = client.get(f"{NOTICES}/{draft['id']}")

    assert detail.status_code == 404
    assert page.url not in detail.content.decode()


def _directory_channel(**flags: Any) -> ComplaintChannel:
    """A directory channel of the network provider (AS 64500) `make_hosting_record` records."""
    provider = DirectoryProvider.objects.create(name="Example Hosting Directory")
    ProviderIdentifier.objects.create(
        provider=provider, kind=ProviderIdentifier.Kind.ASN, value="64500"
    )
    return ComplaintChannel.objects.create(
        provider=provider,
        kind=ComplaintChannel.Kind.ABUSE_EMAIL,
        email="abuse@hosting.example",
        source_url="https://hosting.example/legal",
        checked_at=timezone.now(),
        **flags,
    )


def test_the_forwarding_warning_cannot_be_declared_away_by_the_request(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    """Whether a provider forwards a complaint to the site operator, or publishes it to a
    transparency database, is a fact staff sourced in the directory (the database insists on a
    source URL for it) and it decides whether the customer must confirm the forwarding warning
    before approving. A request that states those flags itself could waive its own warning, so
    they are read from the named channel and never from the body."""
    reference, page = ready_original
    channel = _directory_channel(
        forwards_to_site_operator=True,
        forwarding_source_url="https://hosting.example/forwarding",
        publishes_to_transparency_db=True,
        transparency_source_url="https://hosting.example/transparency",
    )

    response = _create_with(
        client,
        _draft_body(
            public_id_of(reference),
            page.public_id,
            page.site.domain,
            complaint_channel_id=channel.pk,
            forwards_to_site_operator=False,
            publishes_to_transparency_db=False,
        ),
    )

    assert response.status_code == 200, response.content
    draft = cast(dict[str, Any], response.json())
    assert "forwarding_warning" in [row["kind"] for row in draft["required_confirmations"]]
    without_warning = [
        row for row in _confirmations() if row["kind"] != ConfirmationKind.FORWARDING_WARNING
    ]
    approved = client.post(
        f"{NOTICES}/{draft['id']}/approve", without_warning, content_type="application/json"
    )
    assert approved.status_code == 409
    assert approved.json() == {"detail": "confirmation_incomplete"}


def test_a_channel_the_directory_does_not_offer_for_this_host_is_refused(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    """An unknown id must not reach the foreign key (it raised `IntegrityError`, i.e. a 500 and an
    existence oracle for staff directory rows), and another provider's channel must not lend this
    draft its practice flags."""
    reference, page = ready_original
    elsewhere = DirectoryProvider.objects.create(name="Unrelated Provider")
    ProviderIdentifier.objects.create(
        provider=elsewhere, kind=ProviderIdentifier.Kind.ASN, value="64999"
    )
    other = ComplaintChannel.objects.create(
        provider=elsewhere,
        kind=ComplaintChannel.Kind.ABUSE_EMAIL,
        email="abuse@unrelated.example",
        source_url="https://unrelated.example/legal",
        checked_at=timezone.now(),
        forwards_to_site_operator=False,
        forwarding_source_url="https://unrelated.example/forwarding",
    )

    for channel_id in (987654, other.pk):
        response = _create_with(
            client,
            _draft_body(
                public_id_of(reference),
                page.public_id,
                page.site.domain,
                complaint_channel_id=channel_id,
            ),
        )

        assert response.status_code == 409, (channel_id, response.status_code)
        assert response.json() == {"detail": "unknown_channel"}


def test_a_draft_can_only_be_addressed_to_a_host_it_reports(
    client: Client, ready_original: tuple[Reference, Page]
) -> None:
    """`recipient.hostname` was free text, and a provider response recorded on a draft writes
    `catalog.HosterContact` — the rounded response history *other* organisations are shown
    (ADR 0027). A caller could therefore have pointed a draft at any provider in the directory
    and fed that provider's statistics from a finding of their own on a different host."""
    reference, page = ready_original
    make_hosting_record("unrelated-hoster.example")

    response = _create_with(
        client,
        _draft_body(public_id_of(reference), page.public_id, "unrelated-hoster.example"),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "unknown_recipient"}
    assert HosterContact.objects.count() == 0
