"""The photo-collection parity fixture is the contract between this hasher and the browser's.

Issue #116, ADR 0053. `matching/tests/fixtures/photos/collection.json` holds synthetic images (raw
RGB, base64) and the document `fingerprint_photos` wrote for them. This half asserts Python still
produces exactly that from exactly those pixels; `frontend/src/lib/photoFingerprint.test.ts` reads
the same file and asserts the browser code does too — same eight hashes per image, same quality,
same ordinals, same collection digest.

**Never weaken an assertion here.** Hashes that drift silently look like a matching failure rather
than a build failure, which is the reason ADR 0017 put the video fixtures in one place to begin
with. Regenerate deliberately with `uv run python -m matching.tests.generate_photo_fixtures`, and
only together with a new pipeline tag if the recipe itself changed.
"""

import base64
import json
from pathlib import Path
from typing import Any

import numpy as np
import pdqhash

from matching.fingerprint import (
    KIND_PHOTOS,
    PHOTO_PIPELINE,
    collection_sha256,
)
from matching.pdq import MIN_QUALITY

FIXTURE = Path(__file__).parent / "fixtures" / "photos" / "collection.json"


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text("utf-8"))
    return data


def _picture(image: dict[str, Any]) -> np.ndarray:
    pixels = np.frombuffer(base64.b64decode(image["rgb_base64"]), dtype=np.uint8)
    return pixels.reshape(image["height"], image["width"], 3)


def test_the_document_is_a_photo_collection_on_the_operators_own_recipe() -> None:
    document = _fixture()["document"]

    assert (document["kind"], document["pipeline"]) == (KIND_PHOTOS, PHOTO_PIPELINE)
    assert document["image_count"] == 4
    # A collection carries no time anywhere: not a duration, not a rate, not a timestamp.
    assert "duration_s" not in document and "fps" not in document
    assert all("timestamp_s" not in frame for frame in document["frames"])


def test_python_still_produces_these_hashes_from_these_pixels() -> None:
    fixture = _fixture()
    frames = fixture["document"]["frames"]

    for image in fixture["images"]:
        variants, quality = pdqhash.compute_dihedral(np.ascontiguousarray(_picture(image)))
        hashes = [
            np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes().hex() for bits in variants
        ]
        stored = sorted(
            (frame for frame in frames if frame["frame_index"] == image["frame_index"]),
            key=lambda frame: frame["dihedral"],
        )
        if quality < MIN_QUALITY:
            # A featureless image keeps its ordinal and contributes nothing (ADR 0020).
            assert stored == []
            continue
        assert [frame["pdq"] for frame in stored] == hashes
        assert {frame["quality"] for frame in stored} == {int(quality)}


def test_the_collection_digest_is_the_digest_of_its_images() -> None:
    fixture = _fixture()

    digest = collection_sha256(image["sha256"] for image in fixture["images"])

    assert digest == fixture["document"]["sha256"]
    # Order-independent: the identity does not depend on the order the files were listed in.
    assert collection_sha256(image["sha256"] for image in reversed(fixture["images"])) == digest
