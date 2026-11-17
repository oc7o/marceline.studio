"""`/api/v1/app/uploads`: cloud fingerprinting's customer API (issue #59, ADR 0044).

The claims worth keeping executable are the ones a reader of the code cannot check: that the whole
feature is invisible while the switch is off, that a foreign upload is indistinguishable from an
unknown one, that every cap really refuses, and that a chunk sequence with a gap in it is refused
rather than silently hashed as zeros.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from core import public_ids
from marceline.app.rights import CURRENT_VERSION
from marceline.app.upload_declaration import CURRENT_VERSION as DECLARATION_VERSION
from marceline.models import MediaUpload, UploadAttempt
from marceline.tests.app_helpers import APP, add_member, client_for, make_organisation

pytestmark = pytest.mark.django_db

URL = f"{APP}/uploads"
UNKNOWN_UPLOAD = public_ids.generate("upl")
CHUNK = 1024


@pytest.fixture
def spooled(tmp_path: Path, settings: Settings) -> Path:
    settings.CLOUD_FINGERPRINTING_ENABLED = True
    settings.CLOUD_UPLOAD_SPOOL_DIR = str(tmp_path / "spool")
    settings.CLOUD_UPLOAD_CHUNK_BYTES = CHUNK
    settings.CLOUD_UPLOAD_MAX_BYTES = 10 * CHUNK
    settings.CLOUD_UPLOAD_SPOOL_MAX_BYTES = 100 * CHUNK
    settings.CLOUD_UPLOAD_DAILY_BYTES_PER_ORG = 50 * CHUNK
    return tmp_path / "spool"


@pytest.fixture
def organisation() -> Organisation:
    return make_organisation("Studio A")


@pytest.fixture
def customer(organisation: Organisation) -> Client:
    return client_for(add_member(organisation, "customer-a"))


def start(client: Client, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "file_size": 3 * CHUNK,
        "title": "An original",
        "creators": ["A Creator"],
        "fps": 1.0,
        "rights_confirmed": True,
        "rights_text_version": CURRENT_VERSION,
        "language": "en",
        "sole_performer_confirmed": True,
        "article9_consent": True,
        "upload_declaration_version": DECLARATION_VERSION,
    }
    payload.update(overrides)
    return client.post(URL, payload, content_type="application/json")


def send_chunk(client: Client, upload_id: str, index: int, data: bytes) -> Any:
    return client.post(
        f"{URL}/{upload_id}/chunks/{index}", data=data, content_type="application/octet-stream"
    )


def test_the_whole_feature_is_invisible_while_the_switch_is_off(
    customer: Client, settings: Settings, tmp_path: Path
) -> None:
    settings.CLOUD_FINGERPRINTING_ENABLED = False
    settings.CLOUD_UPLOAD_SPOOL_DIR = str(tmp_path / "spool")
    assert start(customer).status_code == 404
    assert customer.get(URL).status_code == 404
    assert customer.get(f"{URL}/{UNKNOWN_UPLOAD}").status_code == 404
    assert MediaUpload.objects.count() == 0


def test_a_switch_without_a_spool_is_still_off(customer: Client, settings: Settings) -> None:
    """Half-configured is off, like the rights vault: nothing falls back to a default directory."""
    settings.CLOUD_FINGERPRINTING_ENABLED = True
    settings.CLOUD_UPLOAD_SPOOL_DIR = ""
    assert start(customer).status_code == 404


def test_me_says_whether_this_machine_takes_files(
    customer: Client, spooled: Path, settings: Settings
) -> None:
    assert customer.get(f"{APP}/me").json()["can_upload_media"] is True
    settings.CLOUD_FINGERPRINTING_ENABLED = False
    assert customer.get(f"{APP}/me").json()["can_upload_media"] is False


def test_an_upload_runs_from_start_to_queued(customer: Client, spooled: Path) -> None:
    response = start(customer)
    assert response.status_code == 201
    upload = response.json()
    assert upload["chunk_bytes"] == CHUNK
    assert upload["next_chunk"] == 0 and upload["status"] == "receiving"

    data = bytes(range(256)) * 12  # 3 chunks exactly
    for index in range(3):
        answer = send_chunk(
            customer, upload["id"], index, data[index * CHUNK : (index + 1) * CHUNK]
        )
        assert answer.status_code == 200, answer.json()
        assert answer.json()["next_chunk"] == index + 1

    done = customer.post(
        f"{URL}/{upload['id']}/complete",
        {"sha256": hashlib.sha256(data).hexdigest()},
        content_type="application/json",
    )
    assert done.status_code == 200, done.json()
    assert done.json()["status"] == "queued"
    job = MediaUpload.objects.get()
    assert job.sha256 == hashlib.sha256(data).hexdigest()
    assert job.received_bytes == len(data)
    # The customer's file name never travelled: only the title they typed.
    assert job.title == "An original"


def test_a_chunk_out_of_order_is_refused_rather_than_hashed_as_zeros(
    customer: Client, spooled: Path
) -> None:
    upload = start(customer).json()
    assert send_chunk(customer, upload["id"], 2, b"x" * CHUNK).status_code == 409
    assert MediaUpload.objects.get().received_bytes == 0


def test_resending_the_last_chunk_is_free(customer: Client, spooled: Path) -> None:
    upload = start(customer).json()
    assert send_chunk(customer, upload["id"], 0, b"a" * CHUNK).status_code == 200
    again = send_chunk(customer, upload["id"], 0, b"b" * CHUNK)
    assert again.status_code == 200
    assert again.json()["received_bytes"] == CHUNK


def test_a_digest_that_does_not_match_empties_the_spool_and_queues_nothing(
    customer: Client, spooled: Path
) -> None:
    upload = start(customer, file_size=CHUNK).json()
    send_chunk(customer, upload["id"], 0, b"a" * CHUNK)
    answer = customer.post(
        f"{URL}/{upload['id']}/complete",
        {"sha256": "0" * 64},
        content_type="application/json",
    )
    assert answer.status_code == 400
    assert answer.json()["detail"] == "digest_mismatch"
    job = MediaUpload.objects.get()
    assert job.status == MediaUpload.Status.REFUSED
    assert job.bytes_deleted_at is not None
    assert not (spooled / upload["id"]).exists()


def test_more_bytes_than_declared_are_refused(customer: Client, spooled: Path) -> None:
    upload = start(customer, file_size=CHUNK).json()
    send_chunk(customer, upload["id"], 0, b"a" * CHUNK)
    assert send_chunk(customer, upload["id"], 1, b"a" * CHUNK).status_code == 413


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"rights_confirmed": False}, "rights_confirmation_required"),
        ({"rights_text_version": "not-a-version"}, "rights_confirmation_required"),
        ({"file_size": 0}, "too_large"),
        ({"file_size": 999 * CHUNK}, "too_large"),
        ({"fps": 7.0}, "invalid_request"),
        ({"creators": []}, "invalid_request"),
        ({"title": "   "}, "invalid_request"),
    ],
)
def test_what_is_refused_before_a_byte_arrives(
    customer: Client, spooled: Path, overrides: dict[str, Any], code: str
) -> None:
    response = start(customer, **overrides)
    assert response.status_code in (400, 503)
    assert response.json()["detail"] == code
    assert MediaUpload.objects.count() == 0


def test_the_daily_byte_quota_refuses_the_one_that_would_exceed_it(
    customer: Client, spooled: Path, settings: Settings
) -> None:
    settings.CLOUD_UPLOAD_DAILY_BYTES_PER_ORG = 4 * CHUNK
    assert start(customer, file_size=3 * CHUNK).status_code == 201
    refused = start(customer, file_size=3 * CHUNK)
    assert refused.status_code == 503 and refused.json()["detail"] == "quota_exceeded"


def test_a_full_spool_refuses_rather_than_filling_the_disk(
    customer: Client, spooled: Path, settings: Settings
) -> None:
    settings.CLOUD_UPLOAD_SPOOL_MAX_BYTES = CHUNK
    upload = start(customer, file_size=CHUNK).json()
    send_chunk(customer, upload["id"], 0, b"a" * CHUNK)
    refused = start(customer, file_size=CHUNK)
    assert refused.status_code == 503 and refused.json()["detail"] == "spool_full"


def test_the_per_organisation_rate_limit_counts_an_upload_like_any_other_registration(
    customer: Client, spooled: Path, organisation: Organisation
) -> None:
    assert start(customer).status_code == 201
    assert (
        UploadAttempt.objects.filter(
            organisation=organisation, kind=UploadAttempt.Kind.ORIGINAL
        ).count()
        == 1
    )


def test_another_organisations_upload_is_the_same_404_as_an_unknown_one(
    customer: Client, spooled: Path
) -> None:
    other = client_for(add_member(make_organisation("Studio B"), "customer-b"))
    theirs = start(other).json()["id"]
    unknown = customer.get(f"{URL}/{UNKNOWN_UPLOAD}")
    foreign = customer.get(f"{URL}/{theirs}")
    malformed = customer.get(f"{URL}/not-an-id")
    assert unknown.status_code == foreign.status_code == malformed.status_code == 404
    assert unknown.content == foreign.content


def test_a_customer_sees_only_their_own_uploads(customer: Client, spooled: Path) -> None:
    mine = start(customer).json()["id"]
    other = client_for(add_member(make_organisation("Studio B"), "customer-b"))
    start(other)
    assert [row["id"] for row in customer.get(URL).json()] == [mine]


def test_cancelling_takes_the_bytes_immediately(customer: Client, spooled: Path) -> None:
    upload = start(customer, file_size=CHUNK).json()
    send_chunk(customer, upload["id"], 0, b"a" * CHUNK)
    assert (spooled / upload["id"]).exists()
    answer = customer.delete(f"{URL}/{upload['id']}")
    assert answer.status_code == 200 and answer.json()["status"] == "cancelled"
    assert not (spooled / upload["id"]).exists()
    assert MediaUpload.objects.get().bytes_deleted_at is not None


def test_an_upload_needs_a_session(spooled: Path) -> None:
    assert Client().post(URL, {}, content_type="application/json").status_code == 401


def test_an_account_without_an_organisation_is_refused(spooled: Path) -> None:
    from django.contrib.auth.models import User

    user = User.objects.create_user("nobody", password="secret-pass-123")
    answer = start(client_for(user))
    assert answer.status_code == 403 and answer.json()["detail"] == "no_organisation"


def _real_clip(tmp_path: Path) -> bytes:
    """A three-second synthetic clip, encoded here and never stored anywhere."""
    from matching.tests.test_frames import make_video

    make_video(tmp_path, "testsrc2=duration=3:size=320x240:rate=10")
    return (tmp_path / "clip.mp4").read_bytes()


def test_a_real_file_becomes_an_original_and_the_bytes_are_gone(
    customer: Client, spooled: Path, organisation: Organisation, settings: Settings, tmp_path: Path
) -> None:
    """The whole chain once, with the real decoder: send a clip, run the worker, and find the
    original in the customer's own account with nothing left in the spool.

    The clip is generated with ffmpeg at test time — no media is ever in the repository — and the
    hashes are the ones `fingerprint_video` produces, so this also shows the registration path is
    the browser upload's (`register_fingerprint`) rather than a second one.
    """
    from marceline.app.ownership import owned_references
    from marceline.uploads import service
    from matching.fingerprint import PIPELINE

    settings.CLOUD_UPLOAD_CHUNK_BYTES = 64 * 1024
    settings.CLOUD_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
    settings.CLOUD_UPLOAD_SPOOL_MAX_BYTES = 8 * 1024 * 1024
    settings.CLOUD_UPLOAD_DAILY_BYTES_PER_ORG = 8 * 1024 * 1024
    data = _real_clip(tmp_path)

    upload = start(customer, file_size=len(data)).json()
    chunk = upload["chunk_bytes"]
    for index in range((len(data) + chunk - 1) // chunk):
        answer = send_chunk(
            customer, upload["id"], index, data[index * chunk : (index + 1) * chunk]
        )
        assert answer.status_code == 200, answer.json()
    done = customer.post(
        f"{URL}/{upload['id']}/complete",
        {"sha256": hashlib.sha256(data).hexdigest()},
        content_type="application/json",
    )
    assert done.status_code == 200, done.json()

    job = service.claim()
    assert job is not None
    assert service.run_job(job) == "created"

    job.refresh_from_db()
    assert job.status == MediaUpload.Status.DONE
    assert job.original_id.startswith("orig_")
    assert job.bytes_deleted_at is not None
    assert not (spooled / upload["id"]).exists()
    # Trial finding 1: the throttle used to drop the decode's last update, so a done job could
    # settle on a stale count (23 of 30 moments). A finished bar always reaches its own total.
    assert job.frames_total is not None
    assert job.frames_done == job.frames_total

    [reference] = list(owned_references(organisation))
    assert reference.sha256 == hashlib.sha256(data).hexdigest()
    assert reference.pipeline == PIPELINE
    assert reference.title == "An original"
    assert reference.frames.exists()

    # And the status the screen polls now names the original and says the file is gone.
    status = customer.get(f"{URL}/{upload['id']}").json()
    assert status["status"] == "done"
    assert status["original_id"] == job.original_id
    assert status["reason"] is None and status["bytes_deleted"] is True


# --- the status the screen polls (ADR 0044 amendment 2026-09-18) ------------------------------


def _job_of(customer: Client) -> MediaUpload:
    """One upload in flight, as `start` leaves it."""
    start(customer)
    return MediaUpload.objects.get()


def test_the_status_carries_the_bar_and_nothing_to_read_it_by(
    customer: Client, spooled: Path
) -> None:
    """A status answer is the customer's own numbers and codes: no file name — there never was
    one — no path, no decoder text, nothing built from the file."""
    job = _job_of(customer)
    job.status = MediaUpload.Status.HASHING
    job.frames_done, job.frames_total = 12, 60
    job.save(update_fields=["status", "frames_done", "frames_total"])

    body = customer.get(f"{URL}/{job.public_id}").json()
    assert body["status"] == "hashing"
    assert (body["frames_done"], body["frames_total"]) == (12, 60)
    assert body["reason"] is None and body["original_id"] is None
    assert body["bytes_deleted"] is False
    assert set(body) == {
        "id",
        "title",
        "status",
        "code",
        "reason",
        "original_id",
        "declared_bytes",
        "received_bytes",
        "chunk_bytes",
        "next_chunk",
        "frames_done",
        "frames_total",
        "bytes_deleted",
        "created_at",
    }


def test_a_total_nobody_knows_yet_is_null_rather_than_a_guess(
    customer: Client, spooled: Path
) -> None:
    job = _job_of(customer)
    job.status = MediaUpload.Status.QUEUED
    job.save(update_fields=["status"])

    body = customer.get(f"{URL}/{job.public_id}").json()
    assert body["status"] == "queued"
    assert body["frames_total"] is None and body["frames_done"] == 0


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (MediaUpload.Status.REFUSED, "unsupported_codec", "unsupported_format"),
        (MediaUpload.Status.REFUSED, "unsupported_container", "unsupported_format"),
        (MediaUpload.Status.REFUSED, "no_video_stream", "unsupported_format"),
        (MediaUpload.Status.REFUSED, "too_long", "too_long"),
        (MediaUpload.Status.REFUSED, "frame_too_large", "too_large"),
        (MediaUpload.Status.REFUSED, "decode_failed", "decode_failed"),
        (MediaUpload.Status.REFUSED, "decode_timeout", "timed_out"),
        (MediaUpload.Status.REFUSED, "digest_mismatch", "incomplete"),
        (MediaUpload.Status.EXPIRED, "expired", "incomplete"),
        (MediaUpload.Status.REFUSED, "claim_needs_review", "needs_review"),
        (MediaUpload.Status.CANCELLED, "cancelled", "cancelled"),
        (MediaUpload.Status.FAILED, "processing_failed", "internal"),
        # A code nobody has worded yet reads as `internal` rather than reaching a screen.
        (MediaUpload.Status.FAILED, "something_new", "internal"),
    ],
)
def test_every_ending_has_a_reason_from_the_closed_set(
    customer: Client, spooled: Path, status: str, code: str, reason: str
) -> None:
    job = _job_of(customer)
    job.status, job.result_code = status, code
    job.save(update_fields=["status", "result_code"])

    body = customer.get(f"{URL}/{job.public_id}").json()
    assert body["reason"] == reason
    assert body["status"] == status


def test_an_upload_still_running_has_no_reason(customer: Client, spooled: Path) -> None:
    job = _job_of(customer)
    for status in (
        MediaUpload.Status.RECEIVING,
        MediaUpload.Status.QUEUED,
        MediaUpload.Status.HASHING,
    ):
        job.status = status
        job.save(update_fields=["status"])
        assert customer.get(f"{URL}/{job.public_id}").json()["reason"] is None


def test_the_file_is_only_called_deleted_once_it_verifiably_is(
    customer: Client, spooled: Path
) -> None:
    """`bytes_deleted` is `bytes_deleted_at`, which `spool.delete` writes only after checking the
    directory is gone. The screen's sentence about the file hangs on this one flag."""
    from django.utils import timezone

    job = _job_of(customer)
    job.status, job.result_code = MediaUpload.Status.FAILED, "processing_failed"
    job.save(update_fields=["status", "result_code"])
    assert customer.get(f"{URL}/{job.public_id}").json()["bytes_deleted"] is False

    job.bytes_deleted_at = timezone.now()
    job.save(update_fields=["bytes_deleted_at"])
    assert customer.get(f"{URL}/{job.public_id}").json()["bytes_deleted"] is True


def test_the_list_carries_the_same_shape_for_every_open_upload(
    customer: Client, spooled: Path
) -> None:
    """The overview reads the list, not one id at a time, so it has to carry the same facts."""
    job = _job_of(customer)
    job.status, job.frames_done, job.frames_total = MediaUpload.Status.HASHING, 3, 9
    job.save(update_fields=["status", "frames_done", "frames_total"])

    [row] = customer.get(URL).json()
    assert (row["id"], row["status"], row["frames_done"], row["frames_total"]) == (
        job.public_id,
        "hashing",
        3,
        9,
    )


def test_another_organisations_status_is_the_same_404_as_an_unknown_one(
    customer: Client, spooled: Path
) -> None:
    other = make_organisation("Studio B")
    theirs = client_for(add_member(other, "customer-b"))
    job = _job_of(customer)

    assert theirs.get(f"{URL}/{job.public_id}").status_code == 404
    assert theirs.get(f"{URL}/{UNKNOWN_UPLOAD}").status_code == 404
    assert theirs.get(f"{URL}/not-an-id").status_code == 404
