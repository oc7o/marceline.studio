"""The 404-never-403 rule on the customer API's **actions**, not only its reads.

`test_app_tenancy.py` walks the read model — originals, cases, findings, removals, invoices. The
routes that *do* something take an id too, and each one of them is a place where a foreign row
could be confirmed to exist: the notice-draft actions (approve, mark as sent, sandbox send, record
a response, withdraw, correct), the rights writes that name a work, and the submissions list that
shows what production accepted. They were each written with the rule in mind — this file is the
test that says so, endpoint by endpoint, so a new action cannot quietly be the one that answers
403, or 409, or an empty 200 that reads as "no such thing, but I looked".

Every assertion here is the same shape: what organisation B is told about organisation A's row must
be **byte-identical** to what it is told about an id that was never issued.
"""

from collections.abc import Callable
from typing import Any, cast

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Organisation
from delivery.models import OutboxItem
from index.models import Page
from marceline.notices.services import unknown_notice_public_id
from marceline.rights.declarations import (
    CURRENT_AUTHORITY_DECLARATION,
    CURRENT_AUTHORITY_WITHDRAWAL,
    CURRENT_WITHDRAWAL,
)
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
from marceline.tests.test_app_originals import match
from matching.models import Reference

NOTICES = f"{APP}/notices"
RIGHTS = f"{APP}/rights"
UNKNOWN_NOTICE = unknown_notice_public_id()


@pytest.fixture
def organisation_a(db: None) -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def owner_a(organisation_a: Organisation) -> User:
    return add_member(organisation_a, "alpha-owner")


@pytest.fixture
def owner_b(db: None) -> User:
    return add_member(make_organisation("Bravo Creator"), "bravo-owner")


@pytest.fixture
def staff(db: None) -> User:
    return User.objects.create_user("reviewer", password="secret-pass-123", is_staff=True)


@pytest.fixture
def original_a(organisation_a: Organisation, owner_a: User, staff: User) -> Reference:
    reference = make_reference(owner_a, title="Alpha's own original")
    rights_ready(organisation_a, reference.sha256, owner_a, staff)
    return reference


@pytest.fixture
def draft_a(
    organisation_a: Organisation,
    owner_a: User,
    original_a: Reference,
    make_page: Callable[[str], Page],
) -> dict[str, Any]:
    """One notice draft of organisation A, prepared exactly as its own screen prepares it."""
    page = playable(make_page("alpha-candidate"))
    make_hosting_record(page.site.domain)
    match(
        original_a,
        page,
        band="high",
        status="confirmed",
        copy_extent="full_copy",
        copy_extent_version=1,
    )
    response = client_for(owner_a).post(
        NOTICES,
        {
            "original_id": public_id_of(original_a),
            "page_ids": [page.public_id],
            "recipient": {
                "hostname": page.site.domain,
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


#: Every notice action, as (suffix, body). `approve` takes a list, the others an object; all of
#: them must refuse a draft of another organisation the way they refuse one that never existed.
NOTICE_ACTIONS: tuple[tuple[str, Any], ...] = (
    ("{id}/approve", []),
    ("{id}/mark-sent", {}),
    # The sandbox send puts its verb before the id, so the prefix allowlist can keep it off the
    # public host (ADR 0046). The tenancy rule is the same either way.
    ("sandbox-send/{id}", {"recipient_email": "sandbox@example.test"}),
    ("{id}/response", {"outcome": "removed"}),
    ("{id}/withdraw", {"reason": "mistake"}),
    ("{id}/correct", {"reason": "mistake"}),
)


@pytest.mark.parametrize(("suffix", "body"), NOTICE_ACTIONS)
def test_a_notice_action_on_another_organisations_draft_is_the_unknown_id_404(
    owner_b: User, draft_a: dict[str, Any], suffix: str, body: Any
) -> None:
    client = client_for(owner_b)

    foreign = client.post(
        f"{NOTICES}/{suffix.format(id=draft_a['id'])}", body, content_type="application/json"
    )
    missing = client.post(
        f"{NOTICES}/{suffix.format(id=UNKNOWN_NOTICE)}", body, content_type="application/json"
    )

    assert foreign.status_code == 404
    assert foreign.content == missing.content


@pytest.mark.parametrize(("suffix", "body"), NOTICE_ACTIONS)
def test_a_notice_action_by_a_stranger_changes_nothing(
    owner_a: User, owner_b: User, draft_a: dict[str, Any], suffix: str, body: Any
) -> None:
    """The counterweight: the 404 above is scope, and the draft is still a fresh draft after it."""
    client_for(owner_b).post(
        f"{NOTICES}/{suffix.format(id=draft_a['id'])}", body, content_type="application/json"
    )

    after = client_for(owner_a).get(f"{NOTICES}/{draft_a['id']}")

    assert after.status_code == 200
    assert after.json()["status"] == "draft"
    assert after.json()["approved_text"] is None


def test_a_notice_of_another_organisation_is_not_in_the_list(
    owner_b: User, draft_a: dict[str, Any]
) -> None:
    assert client_for(owner_b).get(NOTICES).json() == []


def test_notice_candidates_of_another_organisations_original_are_the_unknown_id_404(
    owner_b: User, original_a: Reference
) -> None:
    client = client_for(owner_b)

    foreign = client.get(f"{NOTICES}/candidates/{public_id_of(original_a)}")
    missing = client.get(f"{NOTICES}/candidates/{UNKNOWN_ORIGINAL}")

    assert foreign.status_code == 404
    assert foreign.content == missing.content
    assert original_a.title not in foreign.content.decode()


#: The rights writes that name a work. `declaration` is covered in `test_app_rights.py`; these
#: three are the ones that were not.
RIGHTS_WORK_WRITES: tuple[tuple[str, dict[str, object]], ...] = (
    (
        "/withdrawal",
        {
            "reason": "declared_in_error",
            "declaration_version": CURRENT_WITHDRAWAL.version,
            "language": "en",
            "confirmed": True,
        },
    ),
    (
        "/authority",
        {
            "declaration_version": CURRENT_AUTHORITY_DECLARATION.version,
            "language": "en",
            "confirmed": True,
        },
    ),
    (
        "/authority/withdrawal",
        {
            "reason": "declared_in_error",
            "declaration_version": CURRENT_AUTHORITY_WITHDRAWAL.version,
            "language": "en",
            "confirmed": True,
        },
    ),
)


@pytest.mark.parametrize(("suffix", "body"), RIGHTS_WORK_WRITES)
def test_a_rights_write_about_another_organisations_work_is_the_unknown_id_404(
    owner_b: User, original_a: Reference, suffix: str, body: dict[str, object]
) -> None:
    client = client_for(owner_b)

    foreign = client.post(
        f"{RIGHTS}/works/{public_id_of(original_a)}{suffix}", body, content_type="application/json"
    )
    missing = client.post(
        f"{RIGHTS}/works/{UNKNOWN_ORIGINAL}{suffix}", body, content_type="application/json"
    )

    assert foreign.status_code == 404
    assert foreign.content == missing.content


@pytest.mark.parametrize(("suffix", "body"), RIGHTS_WORK_WRITES)
def test_a_rights_write_by_a_stranger_leaves_the_record_alone(
    owner_a: User, owner_b: User, original_a: Reference, suffix: str, body: dict[str, object]
) -> None:
    """The counterweight: a withdrawal aimed at somebody else's work does not withdraw it.

    The 404 is scope, so the record on the other side of it must be exactly what it was — status,
    stamps and all.
    """
    before = client_for(owner_a).get(RIGHTS).json()["works"]

    client_for(owner_b).post(
        f"{RIGHTS}/works/{public_id_of(original_a)}{suffix}", body, content_type="application/json"
    )

    assert client_for(owner_a).get(RIGHTS).json()["works"] == before


def test_submissions_are_scoped_to_the_organisation_that_sent_them(
    organisation_a: Organisation, owner_a: User, owner_b: User
) -> None:
    """`GET /app/fingerprints/submissions` lists what production accepted (ADR 0028's write path).

    A submission carries the title the customer typed, so a leak across the boundary would be
    content, not just existence.
    """
    item, _ = OutboxItem.objects.get_or_create(
        organisation_id=organisation_a.pk,
        user_id=owner_a.pk,
        kind=OutboxItem.Kind.ADD_ORIGINAL,
        defaults={
            "payload": "{}",
            "title": "Alpha's own upload",
            "received_at": timezone.now(),
        },
    )

    submissions = f"{APP}/fingerprints/submissions"

    assert [row["id"] for row in client_for(owner_a).get(submissions).json()] == [item.public_id]
    stranger = client_for(owner_b).get(submissions)
    assert stranger.json() == []
    assert "Alpha's own upload" not in stranger.content.decode()
