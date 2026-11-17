"""`POST /api/v1/originals`: the browser client's hashes-only upload (ADR 0019)."""

from datetime import date
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client

from accounts.models import Organisation
from catalog.models import Creator, CreatorSuppression
from marceline.app.ownership import owned_references, owner_of
from marceline.tests.app_helpers import add_member, make_organisation
from matching.fingerprint import BROWSER_PIPELINE, Fingerprint, FingerprintFrame, to_json
from matching.models import ClientEvent, Reference, ReferenceLeak, ReferenceMatch

ORIGINALS = "/api/v1/originals"
EVENTS = f"{ORIGINALS}/client-events"


def document(
    sha256: str = "ab" * 32,
    creators: tuple[str, ...] = ("Synthetic Creator",),
    *,
    first_hash_byte: int = 1,
) -> dict[str, Any]:
    """A valid browser fingerprint: 3 positions, 8 orientations each, hashes only."""
    frames = tuple(
        FingerprintFrame(
            frame_index=index,
            timestamp_s=float(index),
            dihedral=orientation,
            quality=90,
            pdq=bytes([first_hash_byte + index * 8 + orientation]) * 32,
        )
        for index in range(3)
        for orientation in range(8)
    )
    return to_json(
        Fingerprint(
            sha256=sha256,
            file_size=2048,
            duration_s=3.0,
            fps=1.0,
            dihedral=True,
            pipeline=BROWSER_PIPELINE,
            title="An original",
            creators=creators,
            client={"tool": "marceline-browser 0.1.0"},
            frames=frames,
        )
    )


@pytest.fixture
def creator(db: None) -> Creator:
    return Creator.objects.create(name="Synthetic Creator", aliases=["Alias"])


def post(client: Client, body: dict[str, Any], path: str = ORIGINALS) -> Any:
    if path == ORIGINALS and "fingerprint" in body:
        body = {"organisation_id": None, **body}
    return client.post(path, data=body, content_type="application/json")


def test_requires_staff(client: Client, user_client: Client, creator: Creator) -> None:
    body = {"fingerprint": document()}
    assert post(client, body).status_code == 401
    assert post(user_client, body).status_code == 403
    assert client.get(f"{ORIGINALS}/creators").status_code == 401
    assert not Reference.objects.exists()


def test_valid_upload_creates_the_original(
    staff_client: Client, staff_user: User, creator: Creator
) -> None:
    response = post(staff_client, {"fingerprint": document(), "source_note": "Browser import"})

    assert response.status_code == 200, response.content
    body = response.json()
    reference = Reference.objects.get(pk=body["reference_id"])
    assert body["frames"] == 3
    assert body["hashes"] == 24
    assert body["creator_names"] == ["Synthetic Creator"]
    assert body["uploaded_by"] == staff_user.get_username()
    assert reference.title == "An original"
    assert reference.frame_fps == 1
    assert reference.source_note == "Browser import"
    assert reference.frames.count() == 24
    # Provenance: its own recipe tag and `source: browser`, never mixed with CLI hashes.
    assert reference.pipeline == BROWSER_PIPELINE
    assert reference.client["source"] == "browser"
    # The claim is bound to the account that uploaded it.
    assert reference.uploaded_by == staff_user
    # An upload is a claim: it proposes nothing and confirms nothing.
    assert not ReferenceMatch.objects.exists()
    assert not ReferenceLeak.objects.exists()


def test_several_creators_first_is_the_rights_holder(
    staff_client: Client, creator: Creator
) -> None:
    partner = Creator.objects.create(name="Carla Bremer")

    response = post(
        staff_client,
        {"fingerprint": document(creators=("Alias", "Carla Bremer"))},
    )

    assert response.status_code == 200, response.content
    reference = Reference.objects.get()
    assert reference.creator == creator
    assert response.json()["creator_names"] == ["Synthetic Creator", "Carla Bremer"]
    # set_creators ran inside the import: the primary creator is in the M2M as well.
    assert set(reference.creators.values_list("pk", flat=True)) == {creator.pk, partner.pk}


def test_creators_field_overrides_the_document(staff_client: Client, creator: Creator) -> None:
    partner = Creator.objects.create(name="Carla Bremer")

    response = post(
        staff_client,
        {"fingerprint": document(creators=("Alias",)), "creators": ["Carla Bremer", "Alias"]},
    )

    assert response.status_code == 200, response.content
    reference = Reference.objects.get()
    assert reference.creator == partner
    assert set(reference.creators.values_list("pk", flat=True)) == {creator.pk, partner.pk}


MALFORMED: dict[str, Any] = {
    "unknown format": {"format": "something-else"},
    "missing orientation": "pop",
    "short hash": "hash",
    "low quality": "quality",
    "wrong pipeline": "pipeline",
    "beyond duration": "duration",
}


@pytest.mark.parametrize("name", MALFORMED)
def test_malformed_payload_is_refused_with_a_clear_error(
    staff_client: Client, creator: Creator, name: str
) -> None:
    payload = document()
    change = MALFORMED[name]
    if change == "pop":
        payload["frames"].pop(3)
    elif change == "hash":
        payload["frames"][0]["pdq"] = "ab" * 16
    elif change == "quality":
        for frame in payload["frames"][:8]:
            frame["quality"] = 10
    elif change == "pipeline":
        payload["pipeline"] = "ffmpeg-rgb512-pdq-v0-canvas"
    elif change == "duration":
        # A whole position past the end of the video; the format allows two frames of slack.
        for frame in payload["frames"][16:]:
            frame["frame_index"], frame["timestamp_s"] = 99, 99.0
    else:
        payload = change

    response = post(staff_client, {"fingerprint": payload})

    assert response.status_code == 400, response.content
    assert "Malformed fingerprint" in response.json()["detail"]
    assert not Reference.objects.exists()


def test_non_object_fingerprint_is_refused(staff_client: Client, creator: Creator) -> None:
    """A body that is not even a JSON object never reaches `from_json`: Ninja refuses it first."""
    response = post(staff_client, {"fingerprint": "definitely not a fingerprint"})

    assert response.status_code == 422
    assert not Reference.objects.exists()


def test_unknown_creator_is_refused(staff_client: Client, creator: Creator) -> None:
    response = post(staff_client, {"fingerprint": document(creators=("Nobody",))})

    assert response.status_code == 400
    assert "Unknown or ambiguous creator" in response.json()["detail"]
    assert not Reference.objects.exists()


def test_document_without_a_creator_is_refused(staff_client: Client, creator: Creator) -> None:
    response = post(staff_client, {"fingerprint": document(creators=())})

    assert response.status_code == 400
    assert "No creator" in response.json()["detail"]


def test_same_file_twice_is_refused(staff_client: Client, creator: Creator) -> None:
    assert post(staff_client, {"fingerprint": document()}).status_code == 200

    response = post(staff_client, {"fingerprint": document()})

    assert response.status_code == 409
    body = response.json()
    assert "already registered" in body["detail"]
    assert body["conflicts"][0]["reason"] == "same sha256"
    assert Reference.objects.count() == 1


def test_duplicate_claim_by_another_account_is_refused(
    staff_client: Client, creator: Creator, db: None
) -> None:
    """A second account registering the same material must not silently get its own original."""
    assert post(staff_client, {"fingerprint": document()}).status_code == 200
    other = Client()
    other.force_login(User.objects.create_user("rival", password="secret-pass-123", is_staff=True))

    # Same hashes, different file digest: a re-encode of the very same video.
    response = post(other, {"fingerprint": document(sha256="cd" * 32)})

    assert response.status_code == 409, response.content
    body = response.json()
    assert "already claimed by another account" in body["detail"]
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "reference"
    assert conflict["status"] == "claimed"
    assert "100% of these frame hashes" in conflict["reason"]
    assert "reviewer" in conflict["reason"]
    assert Reference.objects.count() == 1


def test_the_same_account_may_register_its_own_material_again(
    staff_client: Client, creator: Creator
) -> None:
    """A creator re-registering their own video from another master is not a duplicate claim."""
    assert post(staff_client, {"fingerprint": document()}).status_code == 200

    response = post(staff_client, {"fingerprint": document(sha256="cd" * 32)})

    assert response.status_code == 200, response.content
    assert Reference.objects.count() == 2


def test_unrelated_hashes_are_not_a_duplicate_claim(staff_client: Client, creator: Creator) -> None:
    assert post(staff_client, {"fingerprint": document()}).status_code == 200
    other = Client()
    other.force_login(User.objects.create_user("rival", password="secret-pass-123", is_staff=True))

    response = post(other, {"fingerprint": document(sha256="cd" * 32, first_hash_byte=200)})

    assert response.status_code == 200, response.content
    assert Reference.objects.count() == 2


def test_creators_list(staff_client: Client, creator: Creator) -> None:
    Creator.objects.create(name="Carla Bremer")

    rows = staff_client.get(f"{ORIGINALS}/creators").json()

    assert [row["name"] for row in rows] == ["Carla Bremer", "Synthetic Creator"]


def test_the_picker_offers_no_suppressed_and_no_unreviewed_name(
    staff_client: Client, creator: Creator
) -> None:
    """ADR 0038's single rule, `Creator.objects.usable()`, applies to a picker too: offering a
    suppressed name is how it gets linked to an original again."""
    suppressed = Creator.objects.create(name="Carla Bremer")
    CreatorSuppression.objects.create(
        creator=suppressed,
        requested_on=date(2026, 9, 1),
        answer_due_on=date(2026, 10, 1),
        identity_check=CreatorSuppression.IdentityCheck.NOT_VERIFIED,
    )
    Creator.objects.create(name="A Customer Claim", needs_review=True)

    rows = staff_client.get(f"{ORIGINALS}/creators").json()

    assert [row["name"] for row in rows] == ["Synthetic Creator"]


def test_client_events_count_local_failures(staff_client: Client, staff_user: User) -> None:
    first = post(
        staff_client,
        {"kind": "codec_unsupported", "container": "video/quicktime", "codec": "prores"},
        EVENTS,
    )
    second = post(staff_client, {"kind": "codec_unsupported", "detail": "decode error"}, EVENTS)
    post(staff_client, {"kind": "client_failed", "detail": "Cancelled"}, EVENTS)

    assert first.json() == {"kind": "codec_unsupported", "total": 1}
    assert second.json()["total"] == 2
    assert ClientEvent.objects.filter(kind="client_failed").count() == 1
    event = ClientEvent.objects.filter(kind="codec_unsupported").first()
    assert event is not None
    assert (event.container, event.codec, event.user) == ("video/quicktime", "prores", staff_user)


def test_client_events_reject_an_unknown_kind(staff_client: Client, db: None) -> None:
    assert post(staff_client, {"kind": "disk_full"}, EVENTS).status_code == 422
    assert not ClientEvent.objects.exists()


def test_the_owner_must_be_chosen_explicitly(staff_client: Client, creator: Creator) -> None:
    """Issue #76: no default owner — omitting the field is refused, null is a deliberate choice."""
    response = staff_client.post(
        ORIGINALS, data={"fingerprint": document()}, content_type="application/json"
    )
    assert response.status_code == 422
    assert not Reference.objects.exists()

    unassigned = post(staff_client, {"organisation_id": None, "fingerprint": document()})
    assert unassigned.status_code == 200
    assert unassigned.json()["organisation"] is None
    assert owner_of(unassigned.json()["reference_id"]) is None


def test_an_upload_belongs_to_the_chosen_organisation_not_the_uploaders(
    creator: Creator, db: None
) -> None:
    """Review finding M2: a reviewer with a membership registers for a customer, not for itself."""
    own_organisation = make_organisation("Reviewer's Test Org")
    customer = make_organisation("Alpha Studio")
    reviewer = add_member(own_organisation, "reviewer-member", is_staff=True)
    client = Client()
    client.force_login(reviewer)

    response = post(client, {"organisation_id": customer.pk, "fingerprint": document()})

    assert response.status_code == 200, response.content
    assert response.json()["organisation"] == "Alpha Studio"
    reference = Reference.objects.get(pk=response.json()["reference_id"])
    assert reference.uploaded_by == reviewer
    assert list(owned_references(customer)) == [reference]
    assert list(owned_references(own_organisation)) == []


def test_an_unknown_organisation_is_refused(staff_client: Client, creator: Creator) -> None:
    response = post(staff_client, {"organisation_id": 999999, "fingerprint": document()})

    assert response.status_code == 400
    assert response.json() == {"detail": "Unknown organisation"}
    assert not Reference.objects.exists()


def test_organisations_to_choose_from(client: Client, staff_client: Client, db: None) -> None:
    make_organisation("Bravo Creator")
    make_organisation("Alpha Studio", Organisation.Segment.ENTERPRISE)

    assert client.get(f"{ORIGINALS}/organisations").status_code == 401
    rows = staff_client.get(f"{ORIGINALS}/organisations").json()
    assert [(row["name"], row["segment"]) for row in rows] == [
        ("Alpha Studio", "enterprise"),
        ("Bravo Creator", "professional"),
    ]
