""" "Without data retention", made into failing tests (issue #59, ADR 0044 §3).

The product statement is that a customer's uploaded file exists only in the spool and is gone
minutes later. That cannot be proved as a negative, so what is proved here is the process: the
file is gone on **every** exit of the job, a reaper takes what a crash left behind, the deletion
stamp is written only from a verified deletion, and nothing that survives — a database row, a log
line, a backup script — carries the media, its path or anything read out of it.

Each test corresponds to one row of ADR 0044 §3. If one of them is deleted, the sentence the
customer is shown stops being true.
"""

from __future__ import annotations

import logging
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.db import models
from django.utils import timezone
from pytest_django.fixtures import Settings

from accounts.models import Organisation
from marceline.app.rights import CURRENT_VERSION
from marceline.app.upload_declaration import CURRENT_VERSION as DECLARATION_VERSION
from marceline.models import MediaUpload
from marceline.tests.app_helpers import add_member, make_organisation
from marceline.uploads import service, spool
from matching.cloud_hash import CloudHashError

pytestmark = pytest.mark.django_db

REPO_ROOT = Path(__file__).resolve().parents[3]
CHUNK = 1024


@pytest.fixture
def spooled(tmp_path: Path, settings: Settings) -> Path:
    settings.CLOUD_FINGERPRINTING_ENABLED = True
    settings.CLOUD_UPLOAD_SPOOL_DIR = str(tmp_path / "spool")
    settings.CLOUD_UPLOAD_CHUNK_BYTES = CHUNK
    return tmp_path / "spool"


@pytest.fixture
def organisation() -> Organisation:
    return make_organisation("Studio A")


def a_job(organisation: Organisation, *, status: str = MediaUpload.Status.QUEUED) -> MediaUpload:
    """A queued job with real bytes in the spool."""
    job = MediaUpload.objects.create(
        organisation=organisation,
        created_by=add_member(organisation, f"customer-{MediaUpload.objects.count()}"),
        declared_bytes=CHUNK,
        title="An original",
        creators=["A Creator"],
        frame_fps=1.0,
        chunk_bytes=CHUNK,
        rights_text_version=CURRENT_VERSION,
        rights_language="en",
        upload_declaration_version=DECLARATION_VERSION,
        upload_declaration_language="en",
        status=status,
        sha256="ab" * 32,
        received_bytes=CHUNK,
    )
    spool.create(job.public_id)
    spool.append(job.public_id, 0, b"x" * CHUNK, chunk_bytes=CHUNK, max_bytes=CHUNK)
    return job


# --- (d) the file is gone on every exit --------------------------------------------------------


def test_the_file_is_gone_after_a_successful_job(
    spooled: Path, organisation: Organisation, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = a_job(organisation)
    monkeypatch.setattr(service, "hash_file", lambda *a, **k: _document())
    code = service.run_job(job)
    assert code in {"created", "already_present"}, code
    assert not (spooled / job.public_id).exists()
    job.refresh_from_db()
    assert job.bytes_deleted_at is not None


def test_the_file_is_gone_after_a_refusal(
    spooled: Path, organisation: Organisation, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = a_job(organisation)
    monkeypatch.setattr(service, "hash_file", _raising(CloudHashError("unsupported_codec")))
    assert service.run_job(job) == "unsupported_codec"
    assert not (spooled / job.public_id).exists()
    job.refresh_from_db()
    assert job.status == MediaUpload.Status.REFUSED and job.bytes_deleted_at is not None


def test_the_file_is_gone_after_a_timeout(
    spooled: Path, organisation: Organisation, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = a_job(organisation)
    monkeypatch.setattr(service, "hash_file", _raising(CloudHashError("decode_timeout")))
    assert service.run_job(job) == "decode_timeout"
    assert not (spooled / job.public_id).exists()


def test_the_file_is_gone_after_an_unexpected_crash(
    spooled: Path, organisation: Organisation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `finally` has to cover the exception nobody thought of, not only the known ones."""
    job = a_job(organisation)
    monkeypatch.setattr(service, "hash_file", _raising(ZeroDivisionError("unexpected")))
    assert service.run_job(job) == "processing_failed"
    assert not (spooled / job.public_id).exists()
    job.refresh_from_db()
    assert job.status == MediaUpload.Status.FAILED and job.bytes_deleted_at is not None


# --- (e) the reaper is the backstop ------------------------------------------------------------


def test_the_reaper_removes_a_directory_no_row_knows_about(
    spooled: Path, organisation: Organisation, settings: Settings
) -> None:
    """The case the guarantee has to survive: a worker killed mid-decode, or a row that never
    existed. The reaper reads modification times and nothing else."""
    job = a_job(organisation, status=MediaUpload.Status.HASHING)
    MediaUpload.objects.all().delete()
    assert (spooled / job.public_id).exists()
    later = timezone.now() + timedelta(hours=settings.CLOUD_UPLOAD_TTL_HOURS + 1)
    assert spool.reap(now=later) == [job.public_id]
    assert not (spooled / job.public_id).exists()


def test_the_reaper_leaves_an_upload_that_is_still_being_received(
    spooled: Path, organisation: Organisation
) -> None:
    job = a_job(organisation, status=MediaUpload.Status.RECEIVING)
    assert spool.reap() == []
    assert (spooled / job.public_id).exists()


def test_the_worker_marks_what_the_reaper_took(
    spooled: Path, organisation: Organisation, settings: Settings
) -> None:
    from marceline.scheduler import run_upload_spool

    job = a_job(organisation, status=MediaUpload.Status.HASHING)
    settings.CLOUD_UPLOAD_TTL_HOURS = 0.0
    summary = run_upload_spool(None)  # type: ignore[arg-type]
    assert "reaped 1" in summary
    job.refresh_from_db()
    assert job.status == MediaUpload.Status.EXPIRED
    assert job.bytes_deleted_at is not None


# --- (f) the stamp is written only from a verified deletion ------------------------------------


def test_deleted_at_is_only_set_after_the_directory_is_really_gone(
    spooled: Path, organisation: Organisation, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = a_job(organisation)
    monkeypatch.setattr(service, "hash_file", lambda *a, **k: _document())
    monkeypatch.setattr(spool, "delete", lambda public_id: False)
    service.run_job(job)
    job.refresh_from_db()
    assert job.bytes_deleted_at is None, "a failed deletion must not be recorded as a deletion"


def test_delete_answers_false_when_the_id_is_not_one_of_ours(spooled: Path) -> None:
    assert spool.delete("../../etc") is False
    assert spool.delete("not-an-upload-id") is False


# --- (a) outside every backup ------------------------------------------------------------------


def test_backup_scripts_never_name_the_spool() -> None:
    """`infra/backup/*` pushes one Postgres dump and one curated `~/.claude` tarball. The spool is
    a named Docker volume, so it is outside that by construction — this fails if someone ever adds
    a path or a volume sweep that would take it with them."""
    for script in sorted((REPO_ROOT / "infra" / "backup").glob("*.sh")):
        text = script.read_text()
        assert "upload-spool" not in text, script.name
        assert "CLOUD_UPLOAD_SPOOL_DIR" not in text, script.name


# --- (b) never in Postgres ---------------------------------------------------------------------


def test_no_model_field_holds_bytes_or_a_path() -> None:
    """The row is digests, sizes, timestamps and codes. A `FileField`, a `BinaryField` or a field
    called `path` would mean the media, or a way back to it, survives the job."""
    forbidden = (models.FileField, models.ImageField, models.BinaryField)
    for field in MediaUpload._meta.get_fields():
        assert not isinstance(field, forbidden), field.name
        assert "path" not in getattr(field, "name", ""), field.name
        assert "file_name" not in getattr(field, "name", ""), field.name


def test_the_spool_refuses_to_live_inside_the_repository(settings: Settings) -> None:
    settings.CLOUD_UPLOAD_SPOOL_DIR = str(REPO_ROOT / "backend" / "spool")
    assert spool.problems() == ["CLOUD_UPLOAD_SPOOL_DIR must be outside the repository"]


def test_a_relative_spool_is_refused(settings: Settings) -> None:
    settings.CLOUD_UPLOAD_SPOOL_DIR = "spool"
    assert spool.available() is False


def test_a_spool_volume_is_allowed_where_the_code_is_mounted_at_the_top(
    settings: Settings,
) -> None:
    """The container case: `backend/` is bind-mounted at `/app`, so the repository "is" `/`.

    Testing an ancestor against the filesystem root calls every absolute path inside the
    repository, which switched the whole feature off on the development VPS, where the spool is
    the `/var/lib/marceline/upload-spool` volume. The code directory itself is still refused.
    """
    settings.BASE_DIR = "/app"
    settings.CLOUD_UPLOAD_SPOOL_DIR = "/var/lib/marceline/upload-spool"
    assert spool.problems() == []
    settings.CLOUD_UPLOAD_SPOOL_DIR = "/app/spool"
    assert spool.problems() == ["CLOUD_UPLOAD_SPOOL_DIR must be outside the repository"]


def test_an_existing_root_at_the_wrong_mode_is_fixed(settings: Settings, tmp_path: Path) -> None:
    """The container case (trial finding 2): a volume's mount point already exists, at its
    default 0755, before this module ever writes anything under it — `mkdir(exist_ok=True)` does
    not change that. `problems()` (and so `root()`) chmods it back to 0700 when we own it."""
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o755)
    settings.CLOUD_UPLOAD_SPOOL_DIR = str(directory)

    assert spool.problems() == []
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_a_root_we_cannot_fix_is_reported_rather_than_ignored(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the chmod above fails — we are not the directory's owner — that is a real
    misconfiguration and `available()` must say so, not silently keep serving from a 0755 root."""
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o755)
    settings.CLOUD_UPLOAD_SPOOL_DIR = str(directory)

    def refuse(self: Path, mode: int) -> None:
        raise PermissionError("not the owner")

    monkeypatch.setattr(Path, "chmod", refuse)

    assert spool.problems() == [
        "CLOUD_UPLOAD_SPOOL_DIR is mode 0o755, not 0o700, and cannot be fixed (not its owner)"
    ]
    assert spool.available() is False
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


# --- (c) never in a log ------------------------------------------------------------------------


def test_nothing_logs_the_spool_path(
    spooled: Path,
    organisation: Organisation,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing job logs its public id and the exception's class, never where the file was: a log
    line outlives the file, and Sentry's scrubbing keeps messages but drops bodies."""
    job = a_job(organisation)
    monkeypatch.setattr(service, "hash_file", _raising(ZeroDivisionError("unexpected")))
    with caplog.at_level(logging.DEBUG):
        service.run_job(job)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert str(spooled) not in logged
    assert job.public_id in logged


def _raising(error: Exception) -> Any:
    def raise_it(*args: Any, **kwargs: Any) -> Any:
        raise error

    return raise_it


def _document() -> dict[str, Any]:
    """A minimal valid fingerprint document, as the decoder would answer."""
    from matching.fingerprint import PIPELINE, Fingerprint, FingerprintFrame, to_json

    frames = tuple(
        FingerprintFrame(
            frame_index=index,
            timestamp_s=float(index),
            dihedral=orientation,
            quality=90,
            pdq=bytes([index + 1, orientation + 1] + [0] * 30),
        )
        for index in range(3)
        for orientation in range(8)
    )
    return to_json(
        Fingerprint(
            kind="video",
            sha256="ab" * 32,
            file_size=CHUNK,
            duration_s=3.0,
            fps=1.0,
            dihedral=True,
            pipeline=PIPELINE,
            title="An original",
            creators=("A Creator",),
            client={},
            frames=frames,
        )
    )
