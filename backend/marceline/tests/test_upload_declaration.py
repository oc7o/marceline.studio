"""The declaration an upload cannot happen without (ADR 0044 amendment 2026-09-19, issue #59).

Consent from a co-performer we have no relationship with is unobtainable, so the feature is narrow:
the uploader declares they are the only person in the file, and consents explicitly to the
processing of it.

So the claims worth keeping executable are that the declaration is **not optional**, that its two
ticks are **separate**, that what is stored is the **verbatim** text with its version and the moment
it was made, and that the wording never promises a duration.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from marceline.app import upload_declaration
from marceline.app.rights import CURRENT_VERSION
from marceline.models import MediaUpload
from marceline.tests.app_helpers import APP, add_member, client_for, make_organisation
from marceline.uploads import service, spool

pytestmark = pytest.mark.django_db

URL = f"{APP}/uploads"
CHUNK = 1024


@pytest.fixture
def spooled(tmp_path: Path, settings: Settings) -> Path:
    settings.CLOUD_FINGERPRINTING_ENABLED = True
    settings.CLOUD_UPLOAD_SPOOL_DIR = str(tmp_path / "spool")
    settings.CLOUD_UPLOAD_CHUNK_BYTES = CHUNK
    settings.CLOUD_UPLOAD_MAX_BYTES = 100 * CHUNK
    settings.CLOUD_UPLOAD_SPOOL_MAX_BYTES = 1000 * CHUNK
    settings.CLOUD_UPLOAD_DAILY_BYTES_PER_ORG = 500 * CHUNK
    return tmp_path / "spool"


@pytest.fixture
def customer(spooled: Path) -> Client:
    return client_for(add_member(make_organisation("Studio A"), "customer-a"))


def _start(client: Client, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "file_size": CHUNK,
        "title": "An original",
        "creators": ["A Creator"],
        "fps": 1.0,
        "rights_confirmed": True,
        "rights_text_version": CURRENT_VERSION,
        "language": "de",
        "sole_performer_confirmed": True,
        "article9_consent": True,
        "upload_declaration_version": upload_declaration.CURRENT_VERSION,
    }
    payload.update(overrides)
    return client.post(URL, payload, content_type="application/json")


# --- the wording itself -------------------------------------------------------------------------


def test_every_version_carries_both_statements_in_both_languages() -> None:
    assert upload_declaration.CURRENT_VERSION in upload_declaration.TEXTS
    for version, languages in upload_declaration.TEXTS.items():
        assert set(languages) == {"de", "en"}, version
        for language, statements in languages.items():
            assert set(statements) == set(upload_declaration.STATEMENTS), (version, language)
            for key, text in statements.items():
                assert text.strip() == text and len(text) > 40, (version, language, key)


def test_no_version_ever_promises_a_duration_or_guarantees_anything() -> None:
    """The six-hour TTL is a crash backstop, not a schedule (ADR 0044 § 3), and the app's own
    wording test bans a duration for exactly this screen. These words are the server's, so the ban
    is repeated where they live."""
    for version, languages in upload_declaration.TEXTS.items():
        for language, statements in languages.items():
            for key, text in statements.items():
                assert upload_declaration.banned_in(text) == [], (version, language, key)


def test_the_german_text_is_the_operative_one_and_says_it_in_the_first_person() -> None:
    german = upload_declaration.TEXTS[upload_declaration.CURRENT_VERSION]["de"]
    assert upload_declaration.OPERATIVE_LANGUAGE == "de"
    for text in german.values():
        assert text.startswith("Ich "), text[:20]
    # The consent must name what it is consent to, or it is not explicit.
    assert "Art. 9" in german["article9_consent"]
    assert (
        "Art. 9(2)(a) GDPR"
        in (upload_declaration.TEXTS[upload_declaration.CURRENT_VERSION]["en"]["article9_consent"])
    )


def test_the_snapshot_is_both_statements_in_the_order_they_are_shown() -> None:
    snapshot = upload_declaration.snapshot(upload_declaration.CURRENT_VERSION, "de")
    texts = upload_declaration.TEXTS[upload_declaration.CURRENT_VERSION]["de"]
    assert snapshot.index(texts["sole_performer"]) < snapshot.index(texts["article9_consent"])
    assert upload_declaration.texts_of("nope-2000-01-01", "de") is None


# --- the endpoint -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"sole_performer_confirmed": False},
        {"article9_consent": False},
        {"sole_performer_confirmed": False, "article9_consent": False},
        {"upload_declaration_version": ""},
        {"upload_declaration_version": "upload-1999-01-01"},
    ],
)
def test_an_upload_without_the_declaration_is_refused_before_a_byte(
    customer: Client, overrides: dict[str, Any]
) -> None:
    """Two ticks, both required, and the version must be the one that is current — a tick against a
    wording nobody shows any more is not consent to this one."""
    response = _start(customer, **overrides)

    assert response.status_code == 400
    assert response.json()["detail"] == "upload_declaration_required"
    assert MediaUpload.objects.count() == 0


def test_the_declaration_is_stored_verbatim_with_its_version_and_the_moment(
    customer: Client,
) -> None:
    before = timezone.now()

    response = _start(customer, language="de")

    assert response.status_code == 201
    job = MediaUpload.objects.get()
    assert job.upload_declaration_version == upload_declaration.CURRENT_VERSION
    assert job.upload_declaration_language == "de"
    assert job.upload_declaration_text == upload_declaration.snapshot(
        upload_declaration.CURRENT_VERSION, "de"
    )
    assert job.upload_declaration_at is not None and job.upload_declaration_at >= before
    # The stored text is the text, not a key into a file that could be lost.
    assert "Ich erkläre" in job.upload_declaration_text
    assert "Art. 9" in job.upload_declaration_text


def test_the_english_reading_aid_is_stored_when_that_is_what_was_shown(customer: Client) -> None:
    assert _start(customer, language="en").status_code == 201
    job = MediaUpload.objects.get()
    assert job.upload_declaration_language == "en"
    assert job.upload_declaration_text == upload_declaration.snapshot(
        upload_declaration.CURRENT_VERSION, "en"
    )


def test_a_declaration_retired_while_the_file_waited_refuses_the_job(
    customer: Client, spooled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule the rights declaration gets at registration. The bytes still go — the refusal
    happens inside the one `try` whose `finally` deletes them (ADR 0044 § 3 (d))."""
    started = _start(customer)
    upload = started.json()
    customer.post(
        f"{URL}/{upload['id']}/chunks/0",
        data=b"x" * CHUNK,
        content_type="application/octet-stream",
    )
    customer.post(
        f"{URL}/{upload['id']}/complete",
        {"sha256": hashlib.sha256(b"x" * CHUNK).hexdigest()},
        content_type="application/json",
    )
    job = service.claim()
    assert job is not None
    monkeypatch.setattr(upload_declaration, "CURRENT_VERSION", "upload-2099-01-01")

    code = service.run_job(job)

    assert code == "upload_declaration_required"
    job.refresh_from_db()
    assert job.status == MediaUpload.Status.REFUSED
    assert job.bytes_deleted_at is not None
    assert not spool.path_of(job.public_id).exists()
