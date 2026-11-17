"""`POST /api/v1/app/fingerprints`: a customer adds an original from browser fingerprints (#29)."""

import json
from datetime import timedelta
from typing import Any, get_args

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from catalog.models import Creator, CreatorSuppression
from crawler import services as crawler_services
from marceline.app import fingerprints, upload_throttle
from marceline.app.ownership import owner_of
from marceline.app.rights import CURRENT_VERSION, TEXTS, text_of
from marceline.app.schemas import CustomerOriginalKind
from marceline.models import (
    OriginalOwnership,
    OriginalPublicId,
    RefusedClaim,
    RightsConfirmation,
    UploadAttempt,
)
from marceline.targets import targets
from marceline.tests.app_helpers import APP, add_member, client_for, make_organisation, own
from matching.fingerprint import (
    BROWSER_PHOTO_PIPELINE,
    BROWSER_PIPELINE,
    KIND_PHOTOS,
    KIND_VIDEO,
    PHOTO_PIPELINE,
    PIPELINE,
    Fingerprint,
    FingerprintFrame,
    to_json,
)
from matching.models import ClientEvent, Reference, ReferenceLeak, ReferenceMatch

pytestmark = pytest.mark.django_db

URL = f"{APP}/fingerprints"
EVENTS = f"{URL}/client-events"


def document(
    sha256: str = "ab" * 32,
    creators: tuple[str, ...] = ("Sasha Meyer",),
    *,
    first_hash_byte: int = 1,
    positions: int = 3,
    fps: float = 1.0,
    pipeline: str = BROWSER_PIPELINE,
    dihedral: bool = True,
) -> dict[str, Any]:
    """A valid browser fingerprint: synthetic hashes only, eight orientations per position."""
    orientations = 8 if dihedral else 1
    frames = tuple(
        FingerprintFrame(
            frame_index=index,
            timestamp_s=index / fps,
            dihedral=orientation,
            quality=90,
            pdq=bytes([(first_hash_byte + index * 8 + orientation) % 256]) * 32,
        )
        for index in range(positions)
        for orientation in range(orientations)
    )
    return to_json(
        Fingerprint(
            sha256=sha256,
            file_size=4096,
            duration_s=positions / fps,
            fps=fps,
            dihedral=dihedral,
            pipeline=pipeline,
            title="Beach day",
            creators=creators,
            client={"tool": "marceline-browser 0.1.0"},
            frames=frames,
        )
    )


def body(fingerprint: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    return {
        "fingerprint": document() if fingerprint is None else fingerprint,
        "rights_confirmed": True,
        "rights_text_version": CURRENT_VERSION,
        "language": "en",
        **overrides,
    }


def post(client: Client, payload: dict[str, Any], path: str = URL) -> Any:
    return client.post(path, data=payload, content_type="application/json")


@pytest.fixture
def studio() -> Organisation:
    return make_organisation("Novak Studio")


@pytest.fixture
def member(studio: Organisation) -> User:
    return add_member(studio, "member@studio.test")


@pytest.fixture
def customer(member: User) -> Client:
    return client_for(member)


@pytest.fixture
def rival() -> Client:
    return client_for(add_member(make_organisation("Rival"), "member@rival.test"))


def test_a_signed_in_member_adds_an_original_to_their_organisation(
    customer: Client, member: User, studio: Organisation
) -> None:
    response = post(customer, body())

    assert response.status_code == 201, response.content
    reference = Reference.objects.get()
    answer = response.json()
    assert answer["created"] is True
    # The customer's opaque id for this file (ADR 0033), never the reference's primary key.
    public = OriginalPublicId.objects.get(organisation=studio, sha256=reference.sha256)
    assert answer["original"]["id"] == public.public_id
    assert answer["original"]["id"].startswith("orig_")
    assert customer.get(f"{APP}/originals/{public.public_id}").status_code == 200
    assert answer["original"]["title"] == "Beach day"
    assert answer["original"]["state"] == "nothing_found"
    # The owner is the caller's organisation, recorded as a customer registration.
    assert owner_of(reference.pk) == studio
    assert OriginalOwnership.objects.get().assigned_via == OriginalOwnership.Via.CUSTOMER
    # The same import path as every other: its own recipe tag, hashes only, bound to the account.
    assert reference.pipeline == BROWSER_PIPELINE
    assert reference.frame_fps == 1
    assert reference.frames.count() == 24
    assert reference.client["source"] == "browser"
    assert reference.uploaded_by == member
    # An upload is a claim: nothing is matched or confirmed by it.
    assert not ReferenceMatch.objects.exists()
    assert not ReferenceLeak.objects.exists()


def test_the_rights_declaration_is_stored_with_who_when_and_which_words(
    customer: Client, member: User, studio: Organisation
) -> None:
    before = timezone.now()

    response = post(customer, body(language="de"))

    assert response.status_code == 201, response.content
    confirmation = RightsConfirmation.objects.get()
    assert confirmation.reference == Reference.objects.get()
    assert confirmation.confirmed_by == member
    assert confirmation.organisation == studio
    assert confirmation.text_version == CURRENT_VERSION
    assert confirmation.language == "de"
    assert confirmation.confirmed_at >= before
    assert text_of(confirmation.text_version, confirmation.language) == TEXTS[CURRENT_VERSION]["de"]


def test_the_declaration_is_factual_and_exists_in_both_languages() -> None:
    for texts in TEXTS.values():
        assert set(texts) == {"de", "en"}
        for text in texts.values():
            assert "leak" not in text.lower()
            assert "infring" not in text.lower()


@pytest.mark.parametrize(
    "overrides",
    [
        {"rights_confirmed": False},
        {"rights_text_version": "2020-01-01"},
    ],
    ids=["not ticked", "outdated wording"],
)
def test_no_original_without_the_current_rights_declaration(
    customer: Client, overrides: dict[str, Any]
) -> None:
    response = post(customer, body(**overrides))

    assert response.status_code == 400
    assert response.json()["detail"] == "rights_confirmation_required"
    assert not Reference.objects.exists()
    assert not RightsConfirmation.objects.exists()


def test_a_missing_declaration_field_is_refused(customer: Client) -> None:
    payload = body()
    del payload["rights_confirmed"]

    assert post(customer, payload).status_code == 422
    assert not Reference.objects.exists()


def _tamper(key: str, value: Any) -> dict[str, Any]:
    fingerprint = document()
    fingerprint[key] = value
    return fingerprint


def _bad_hash() -> dict[str, Any]:
    fingerprint = document()
    fingerprint["frames"][0]["pdq"] = "zz" * 32
    return fingerprint


def _missing_orientation() -> dict[str, Any]:
    fingerprint = document()
    fingerprint["frames"].pop()
    return fingerprint


@pytest.mark.parametrize(
    ("fingerprint", "code"),
    [
        (_bad_hash(), "invalid_fingerprint"),
        (_missing_orientation(), "invalid_fingerprint"),
        (_tamper("sha256", "AB" * 32), "invalid_fingerprint"),
        (_tamper("file_size", 1 << 50), "invalid_fingerprint"),
        (_tamper("frames", []), "invalid_fingerprint"),
        (document(creators=()), "invalid_fingerprint"),
        (document(creators=("Sasha\x00Meyer",)), "invalid_fingerprint"),
        # A title is carried into an email subject on the sandbox send path (ADR 0035), where a
        # newline is a header separator and Django answers it by raising. Refused here instead.
        (_tamper("title", "Beach day\r\nBcc: someone@example.test"), "invalid_fingerprint"),
        (_tamper("title", "Beach\x00day"), "invalid_fingerprint"),
        (document(pipeline=PIPELINE), "unsupported_fingerprint"),
        (document(dihedral=False), "unsupported_fingerprint"),
        (document(fps=3.0), "unsupported_fingerprint"),
    ],
    ids=[
        "hash not hex",
        "orientation missing",
        "digest not lowercase",
        "absurd file size",
        "no frames",
        "no rights holder",
        "control character in a name",
        "line break in the title",
        "control character in the title",
        "server pipeline tag",
        "one orientation only",
        "unsupported sampling rate",
    ],
)
def test_strict_validation(customer: Client, fingerprint: dict[str, Any], code: str) -> None:
    response = post(customer, body(fingerprint))

    assert response.status_code == 400, response.content
    assert response.json()["detail"] == code
    assert not Reference.objects.exists()


def test_the_denser_pilot_rate_is_accepted(customer: Client) -> None:
    response = post(customer, body(document(fps=2.0, positions=4)))

    assert response.status_code == 201, response.content
    assert Reference.objects.get().frame_fps == 2


def test_too_many_positions_is_refused(customer: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fingerprints, "MAX_POSITIONS", 2)

    response = post(customer, body())

    assert response.status_code == 400
    assert response.json()["detail"] == "too_long"
    assert not Reference.objects.exists()


def test_a_large_document_is_read_past_the_global_limit_for_this_route_only(
    customer: Client, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = body()
    size = len(json.dumps(payload))
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = size // 2

    assert post(customer, payload).status_code == 201

    monkeypatch.setattr(fingerprints, "MAX_BODY_BYTES", size // 2 + 1)
    other = body(document(sha256="cd" * 32, first_hash_byte=100))
    response = post(customer, other)
    assert response.status_code == 413
    assert response.json()["detail"] == "body_too_large"
    # Every other customer route keeps Django's global limit.
    login = Client().post(
        f"{APP}/auth/login",
        data={"username": "x" * size, "password": "y"},
        content_type="application/json",
    )
    assert login.status_code == 400


def test_the_same_file_again_is_answered_with_the_original_already_there(
    customer: Client,
) -> None:
    first = post(customer, body())
    again = post(customer, body())

    assert first.status_code == 201
    assert again.status_code == 200, again.content
    assert again.json()["created"] is False
    assert again.json()["original"]["id"] == first.json()["original"]["id"]
    assert Reference.objects.count() == 1
    assert RightsConfirmation.objects.count() == 1


def test_a_file_the_organisation_already_owns_on_another_recipe_is_not_imported_again(
    customer: Client, studio: Organisation
) -> None:
    creator = Creator.objects.create(name="Sasha Meyer")
    cli = own(
        Reference.objects.create(
            creator=creator,
            title="Beach day (CLI)",
            duration_s=3,
            sha256="ab" * 32,
            frame_fps=1,
            pipeline=PIPELINE,
        ),
        studio,
    )

    response = post(customer, body())

    assert response.status_code == 200, response.content
    assert response.json() == {
        "original": response.json()["original"],
        "created": False,
    }
    public = OriginalPublicId.objects.get(organisation=studio, sha256=cli.sha256)
    assert response.json()["original"]["id"] == public.public_id
    assert Reference.objects.count() == 1


def test_another_organisations_file_needs_a_human_and_is_never_named(
    customer: Client, rival: Client, studio: Organisation, member: User
) -> None:
    creator = Creator.objects.create(name="Someone")
    operator_import = Reference.objects.create(
        creator=creator, title="Unassigned", duration_s=3, sha256="ee" * 32, frame_fps=1
    )
    assert post(rival, body(document(sha256="cd" * 32, first_hash_byte=40))).status_code == 201
    rival_reference = Reference.objects.get(sha256="cd" * 32)

    # The same file another organisation registered.
    same_file = post(customer, body(document(sha256="cd" * 32, first_hash_byte=40)))
    # A re-encode: another digest, but the hashes largely duplicate that original.
    same_hashes = post(customer, body(document(sha256="dd" * 32, first_hash_byte=40)))
    # A file the operator imported and nobody owns yet.
    unassigned = post(customer, body(document(sha256=operator_import.sha256, first_hash_byte=90)))

    for response in (same_file, same_hashes, unassigned):
        assert response.status_code == 409, response.content
        assert b"Rival" not in response.content
    # One answer for all three: which case applied is not the customer's to learn.
    assert same_file.content == same_hashes.content == unassigned.content
    assert same_file.json() == {"detail": "claim_needs_review", "message": ""}
    assert not Reference.objects.filter(ownership__organisation=studio).exists()

    # Staff-only: the reason, organisation and original(s) are recorded, one row per attempt
    # (issue #29 amendment). The customer response above never carries any of this.
    claims = {claim.sha256: claim for claim in RefusedClaim.objects.filter(organisation=studio)}
    assert set(claims) == {"cd" * 32, "dd" * 32, operator_import.sha256}
    assert claims["cd" * 32].reason == RefusedClaim.Reason.OWNED_BY_OTHER
    assert claims["cd" * 32].reference_ids == [rival_reference.pk]
    assert claims["dd" * 32].reason == RefusedClaim.Reason.HASH_OVERLAP
    assert claims["dd" * 32].reference_ids == [rival_reference.pk]
    assert claims["dd" * 32].overlap_share is not None
    assert claims["dd" * 32].overlap_share >= 0.5
    assert claims[operator_import.sha256].reason == RefusedClaim.Reason.UNOWNED_EXISTING
    assert claims[operator_import.sha256].reference_ids == [operator_import.pk]
    for claim in claims.values():
        assert claim.requested_by == member
        assert claim.status == RefusedClaim.Status.OPEN


def test_overlapping_hashes_with_the_organisations_own_original_are_fine(customer: Client) -> None:
    assert post(customer, body(document(sha256="ab" * 32))).status_code == 201

    response = post(customer, body(document(sha256="cd" * 32)))

    assert response.status_code == 201, response.content
    assert Reference.objects.count() == 2


def test_the_cast_links_existing_creators_by_exact_name_only(customer: Client) -> None:
    sasha = Creator.objects.create(name="Sasha Meyer")
    carla = Creator.objects.create(name="Carla Bremer", aliases=["Studio Nord"])

    response = post(
        customer,
        body(document(creators=("sasha meyer", "Carla  Bremer", "Studio Nord"))),
    )

    assert response.status_code == 201, response.content
    reference = Reference.objects.get()
    assert reference.creator == sasha
    new = Creator.objects.get(name="Studio Nord")
    # An alias is never matched: a studio name must not become a performer's claim.
    assert new.needs_review is True
    assert set(reference.creators.values_list("pk", flat=True)) == {sasha.pk, carla.pk, new.pk}
    assert not Creator.objects.get(pk=sasha.pk).needs_review


@pytest.mark.django_db
def test_a_suppressed_name_never_gains_a_second_record_from_another_spelling() -> None:
    """ADR 0038: `usable()` decides which spelling a typed name resolves to, and a name we may not
    use is still that person's row — another spelling of it must not become a new one."""
    suppressed = Creator.objects.create(name="Carla Bremer")
    CreatorSuppression.objects.create(
        creator=suppressed,
        requested_on=timezone.now().date(),
        answer_due_on=timezone.now().date() + timedelta(days=30),
        identity_check=CreatorSuppression.IdentityCheck.NOT_VERIFIED,
    )

    assert fingerprints.creator_named("carla bremer") == suppressed
    assert Creator.objects.filter(name__iexact="carla bremer").count() == 1


@pytest.mark.django_db
def test_a_usable_spelling_wins_over_one_we_may_not_use() -> None:
    Creator.objects.create(name="sasha meyer", needs_review=True)
    usable = Creator.objects.create(name="SASHA MEYER")

    assert fingerprints.creator_named("Sasha Meyer").pk == usable.pk
    assert Creator.objects.filter(name__iexact="sasha meyer").count() == 2


def test_unreviewed_names_are_not_hunted_linked_or_searched(customer: Client) -> None:
    assert post(customer, body(document(creators=("Brand New Name",)))).status_code == 201
    creator = Creator.objects.get(name="Brand New Name")
    assert creator.needs_review

    crawler_services.clear_reference_caches()
    assert creator not in crawler_services.cached_creators()
    assert creator.pk not in {target.creator.pk for target in targets()}

    Creator.objects.filter(pk=creator.pk).update(needs_review=False)
    crawler_services.clear_reference_caches()
    assert creator in crawler_services.cached_creators()
    assert creator.pk in {target.creator.pk for target in targets()}


def test_anonymous_and_organisationless_callers_add_nothing(client: Client) -> None:
    assert post(client, body()).status_code == 401
    staff = client_for(User.objects.create_user("reviewer", password="x", is_staff=True))
    response = post(staff, body())
    assert response.status_code == 403
    assert response.json()["detail"] == "no_organisation"
    assert not Reference.objects.exists()


def test_uploads_are_rate_limited_per_organisation(
    customer: Client, rival: Client, studio: Organisation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(upload_throttle.LIMITS, upload_throttle.Kind.ORIGINAL, 2)

    assert post(customer, body(document(sha256="a1" * 32, first_hash_byte=1))).status_code == 201
    # A malformed upload costs validation too, so it counts.
    assert post(customer, body(rights_confirmed=False)).status_code == 400
    limited = post(customer, body(document(sha256="a3" * 32, first_hash_byte=120)))

    assert limited.status_code == 429
    assert limited.json()["detail"] == "too_many_requests"
    assert 3000 <= int(limited["Retry-After"]) <= 3600
    # A refused attempt is not counted, and another organisation has its own budget.
    assert UploadAttempt.objects.filter(organisation=studio).count() == 2
    assert post(rival, body(document(sha256="b1" * 32, first_hash_byte=200))).status_code == 201


def test_the_window_rolls(studio: Organisation, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(upload_throttle.LIMITS, upload_throttle.Kind.ORIGINAL, 1)
    start = timezone.now()
    kind = upload_throttle.Kind.ORIGINAL

    assert upload_throttle.admit(studio.pk, kind, now=start) is None
    refusal = upload_throttle.admit(studio.pk, kind, now=start + timedelta(minutes=59))
    assert refusal is not None and refusal.retry_after_s == 60
    assert upload_throttle.admit(studio.pk, kind, now=start + timedelta(minutes=61)) is None
    # Rows that left the window are cleaned up as new attempts arrive.
    assert UploadAttempt.objects.count() == 1


def test_production_without_the_outbox_takes_no_uploads(
    customer: Client, settings: Settings
) -> None:
    assert customer.get(f"{APP}/me").json()["can_add_originals"] is True

    settings.MARCELINE_ROLE = "prod"

    assert customer.get(f"{APP}/me").json()["can_add_originals"] is False
    response = post(customer, body())
    assert response.status_code == 503
    assert response.json()["detail"] == "adding_originals_unavailable"
    assert post(customer, {"kind": "client_failed"}, EVENTS).status_code == 503
    assert not Reference.objects.exists()
    assert not UploadAttempt.objects.exists()


def test_an_account_without_organisation_is_not_offered_the_page(db: None) -> None:
    staff = client_for(User.objects.create_user("reviewer", password="x", is_staff=True))

    assert staff.get(f"{APP}/me").json()["can_add_originals"] is False


def test_client_events_are_counted_without_totals(customer: Client, member: User) -> None:
    response = post(
        customer,
        {"kind": "codec_unsupported", "detail": "no decoder", "container": "video/quicktime"},
        EVENTS,
    )

    assert response.status_code == 200, response.content
    # How often other customers fail is not a customer's business: no counter in the answer.
    assert response.json() == {"recorded": True}
    event = ClientEvent.objects.get()
    assert (event.kind, event.user, event.container) == (
        "codec_unsupported",
        member,
        "video/quicktime",
    )


def test_client_event_text_is_bounded(customer: Client) -> None:
    response = post(customer, {"kind": "client_failed", "detail": "x" * 500}, EVENTS)

    assert response.status_code == 422
    assert not ClientEvent.objects.exists()


# --- Photo collections (issue #116, ADR 0053) ---------------------------------------------------


def photo_document(
    sha256: str = "cd" * 32,
    creators: tuple[str, ...] = ("Sasha Meyer",),
    *,
    images: int = 3,
    hashed: int | None = None,
    first_hash_byte: int = 1,
    pipeline: str = BROWSER_PHOTO_PIPELINE,
    dihedral: bool = True,
) -> dict[str, Any]:
    """A valid browser photo collection: no duration, no rate, no timestamp anywhere."""
    orientations = 8 if dihedral else 1
    positions = images if hashed is None else hashed
    frames = tuple(
        FingerprintFrame(
            frame_index=index,
            timestamp_s=float(index),
            dihedral=orientation,
            quality=90,
            pdq=bytes([(first_hash_byte + index * 8 + orientation) % 256]) * 32,
        )
        for index in range(positions)
        for orientation in range(orientations)
    )
    return to_json(
        Fingerprint(
            kind=KIND_PHOTOS,
            sha256=sha256,
            file_size=4096,
            duration_s=None,
            fps=None,
            dihedral=dihedral,
            pipeline=pipeline,
            title="Beach set",
            creators=creators,
            client={"tool": "marceline-browser 0.2.0"},
            frames=frames,
            image_count=images,
        )
    )


def test_the_customer_kinds_are_the_matching_kinds() -> None:
    """`originals.original_out` spells the two kinds as literals, so they are pinned here."""
    assert set(get_args(CustomerOriginalKind)) == {KIND_VIDEO, KIND_PHOTOS}


def test_a_photo_set_is_added_as_one_original_with_no_duration(
    customer: Client, studio: Organisation
) -> None:
    response = post(customer, body(photo_document()))

    assert response.status_code == 201, response.content
    reference = Reference.objects.get()
    assert (reference.kind, reference.pipeline) == (KIND_PHOTOS, BROWSER_PHOTO_PIPELINE)
    # The database constraint `reference_shape_matches_kind` says a collection has neither.
    assert (reference.duration_s, reference.frame_fps) == (None, None)
    assert reference.frames.count() == 3 * 8
    assert owner_of(reference.pk) == studio
    assert response.json()["original"]["kind"] == "photos"
    assert response.json()["original"]["duration_s"] is None


def test_a_photo_set_records_the_same_rights_declaration_as_a_video(customer: Client) -> None:
    post(customer, body(photo_document()))

    confirmation = RightsConfirmation.objects.get()
    assert confirmation.text_version == CURRENT_VERSION
    # One current sentence for both kinds, and it names neither (ADR 0053 §4).
    assert "video" not in (text_of(CURRENT_VERSION, "en") or "")
    assert "Video" not in (text_of(CURRENT_VERSION, "de") or "")


def _photo_tamper(key: str, value: Any) -> dict[str, Any]:
    fingerprint = photo_document()
    fingerprint[key] = value
    return fingerprint


@pytest.mark.parametrize(
    ("fingerprint", "code"),
    [
        (photo_document(pipeline=PHOTO_PIPELINE), "unsupported_fingerprint"),
        (photo_document(pipeline=BROWSER_PIPELINE), "invalid_fingerprint"),
        (photo_document(dihedral=False), "unsupported_fingerprint"),
        (photo_document(creators=()), "invalid_fingerprint"),
        (_photo_tamper("frames", []), "invalid_fingerprint"),
        (photo_document(images=2, hashed=3), "invalid_fingerprint"),
    ],
    ids=[
        "the operator's own photo recipe",
        "a video tag on a photo document",
        "one orientation only",
        "no rights holder",
        "no images",
        "more hashed images than the set has",
    ],
)
def test_strict_photo_validation(customer: Client, fingerprint: dict[str, Any], code: str) -> None:
    response = post(customer, body(fingerprint))

    assert response.status_code == 400, response.content
    assert response.json()["detail"] == code
    assert not Reference.objects.exists()


def test_too_many_images_is_refused_with_its_own_code(
    customer: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fingerprints, "MAX_IMAGES", 2)

    response = post(customer, body(photo_document()))

    assert response.status_code == 400
    # Not `too_long`: the advice is "send fewer photographs", not "shorten the video".
    assert response.json()["detail"] == "too_many_images"
    assert not Reference.objects.exists()


def test_the_same_photo_set_again_imports_nothing(customer: Client) -> None:
    assert post(customer, body(photo_document())).status_code == 201

    response = post(customer, body(photo_document(first_hash_byte=200)))

    assert response.status_code == 200, response.content
    assert response.json()["created"] is False
    assert Reference.objects.count() == 1


def test_a_video_upload_is_unaffected_by_the_photo_path(customer: Client) -> None:
    response = post(customer, body(document()))

    assert response.status_code == 201, response.content
    assert Reference.objects.get().kind == "video"
    assert response.json()["original"]["kind"] == "video"
