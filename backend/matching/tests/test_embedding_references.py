"""The reference side of the second signal: format v4 and attaching sketches (ADR 0037, 0022)."""

from typing import Any

import pytest

from catalog.models import Creator
from matching import fingerprint as fp
from matching.embedding import EMBEDDING_PIPELINE
from matching.models import Reference, ReferenceFrameHash
from matching.references import attach_embeddings, import_fingerprint
from matching.tests.images import synthetic_image
from matching.tests.sketches import random_sketches

SKETCHES = random_sketches(4, seed=91)


def document(**overrides: Any) -> dict[str, Any]:
    """A minimal two-moment video fingerprint with sketches, as the client writes it."""
    data: dict[str, Any] = {
        "format": fp.FORMAT,
        "version": 4,
        "kind": "video",
        "pipeline": fp.PIPELINE,
        "embedding_pipeline": EMBEDDING_PIPELINE,
        "sha256": "b" * 64,
        "file_size": 1234,
        "duration_s": 2.0,
        "fps": 1.0,
        "dihedral": False,
        "title": "an original",
        "creator": ["Original Creator"],
        "client": {"tool": "marceline-fingerprint 0.3.0"},
        "frames": [
            {
                "frame_index": index,
                "timestamp_s": float(index),
                "dihedral": 0,
                "quality": 90,
                "pdq": (bytes([index + 1]) * 32).hex(),
                "embedding": SKETCHES[index].hex(),
            }
            for index in range(2)
        ],
    }
    data.update(overrides)
    return data


# --- the document format ---


def test_a_v4_document_round_trips_with_its_sketches() -> None:
    parsed = fp.from_json(document())
    assert parsed.embedding_pipeline == EMBEDDING_PIPELINE
    assert [frame.embedding for frame in parsed.frames] == [SKETCHES[0], SKETCHES[1]]
    assert fp.from_json(fp.to_json(parsed)) == parsed


def test_a_v3_document_still_reads_and_carries_no_recipe() -> None:
    data = document(version=3)
    del data["embedding_pipeline"]
    for frame in data["frames"]:
        frame.pop("embedding")
    parsed = fp.from_json(data)
    assert parsed.embedding_pipeline == ""
    assert [frame.embedding for frame in parsed.frames] == [None, None]


def test_an_unknown_recipe_is_refused() -> None:
    with pytest.raises(fp.FingerprintFormatError, match="unknown embedding pipeline"):
        fp.from_json(document(embedding_pipeline="mobileclip-s0-fp32-sign512-v2"))


def test_a_sketch_without_a_recipe_is_refused() -> None:
    with pytest.raises(fp.FingerprintFormatError, match="names no recipe"):
        fp.from_json(document(embedding_pipeline=""))


def test_a_declared_recipe_with_a_missing_sketch_is_refused() -> None:
    """Half a second signal is worse than none: a gap reads as 'not similar' downstream."""
    data = document()
    data["frames"][1].pop("embedding")
    with pytest.raises(fp.FingerprintFormatError, match="carry no embedding"):
        fp.from_json(data)


def test_a_sketch_of_the_wrong_length_is_refused() -> None:
    data = document()
    data["frames"][0]["embedding"] = "ab" * 32
    with pytest.raises(fp.FingerprintFormatError, match="128 lowercase hex"):
        fp.from_json(data)


def test_only_the_unflipped_orientation_may_carry_a_sketch() -> None:
    data = document(dihedral=True)
    data["frames"] = [
        {
            "frame_index": 0,
            "timestamp_s": 0.0,
            "dihedral": orientation,
            "quality": 90,
            "pdq": (bytes([orientation + 1]) * 32).hex(),
            "embedding": SKETCHES[0].hex(),
        }
        for orientation in range(8)
    ]
    with pytest.raises(fp.FingerprintFormatError, match="only dihedral 0"):
        fp.from_json(data)


def test_hashing_a_position_puts_the_sketch_only_on_the_unflipped_variant() -> None:
    frames = fp._hash_position(synthetic_image(11), 0, 0.0, True, lambda _: SKETCHES[0])
    assert len(frames) == 8
    assert frames[0].embedding == SKETCHES[0]
    assert {frame.embedding for frame in frames[1:]} == {None}


def test_an_embedding_tag_needs_something_that_computes_one() -> None:
    with pytest.raises(ValueError, match="nothing computes an embedding"):
        fp._embedding_tag(None, EMBEDDING_PIPELINE)
    assert fp._embedding_tag(None, "") == ""
    assert fp._embedding_tag(lambda _: b"", "") == EMBEDDING_PIPELINE
    with pytest.raises(ValueError, match="unknown embedding pipeline"):
        fp._embedding_tag(lambda _: b"", "something-else")


# --- attaching sketches to an original that is already imported ---

pytestmark = pytest.mark.django_db


@pytest.fixture
def imported() -> Reference:
    Creator.objects.create(name="Original Creator")
    plain = document()
    del plain["embedding_pipeline"]
    plain["version"] = 3
    for frame in plain["frames"]:
        frame.pop("embedding")
    return import_fingerprint(
        creator=Creator.objects.get(name="Original Creator"),
        fingerprint=fp.from_json(plain),
        mark_shared=False,
    )


def test_a_fresh_import_stores_the_sketches_and_the_tag() -> None:
    Creator.objects.create(name="Original Creator")
    reference = import_fingerprint(
        creator=Creator.objects.get(name="Original Creator"),
        fingerprint=fp.from_json(document()),
        mark_shared=False,
    )
    assert reference.embedding_pipeline == EMBEDDING_PIPELINE
    stored = list(reference.frames.order_by("frame_index").values_list("embedding", flat=True))
    assert [bytes(value or b"") for value in stored] == [SKETCHES[0], SKETCHES[1]]


def test_attaching_adds_the_sketches_to_the_existing_rows(imported: Reference) -> None:
    """The reference side is free and retroactive: same grid, same identity, sketches added."""
    assert imported.embedding_pipeline == ""
    before = list(imported.frames.order_by("frame_index").values_list("pdq", flat=True))

    attachment = attach_embeddings(fp.from_json(document()))

    assert attachment == (imported.pk, 2)
    imported.refresh_from_db()
    assert imported.embedding_pipeline == EMBEDDING_PIPELINE
    # Not one PDQ hash was rewritten, and no second reference appeared.
    after = list(imported.frames.order_by("frame_index").values_list("pdq", flat=True))
    assert [bytes(value) for value in after] == [bytes(value) for value in before]
    assert Reference.objects.count() == 1
    assert ReferenceFrameHash.objects.count() == 2


def test_attaching_twice_changes_nothing(imported: Reference) -> None:
    attach_embeddings(fp.from_json(document()))
    assert attach_embeddings(fp.from_json(document())) == (imported.pk, 2)
    imported.refresh_from_db()
    assert imported.embedding_pipeline == EMBEDDING_PIPELINE


def test_attaching_refuses_a_file_whose_hashes_differ(imported: Reference) -> None:
    """Parity, not trust: a sketch may not describe a moment the stored hash never saw."""
    data = document()
    data["frames"][1]["pdq"] = (bytes([99]) * 32).hex()
    with pytest.raises(ValueError, match="hash differently"):
        attach_embeddings(fp.from_json(data))
    imported.refresh_from_db()
    assert imported.embedding_pipeline == ""


def test_attaching_refuses_another_sampling_grid(imported: Reference) -> None:
    data = document()
    data["frames"] = data["frames"][:1]
    with pytest.raises(ValueError, match="not the same sampling grid"):
        attach_embeddings(fp.from_json(data))


def test_attaching_refuses_a_file_without_sketches(imported: Reference) -> None:
    data = document(embedding_pipeline="")
    for frame in data["frames"]:
        frame.pop("embedding")
    with pytest.raises(ValueError, match="carries no embeddings"):
        attach_embeddings(fp.from_json(data))


def test_attaching_refuses_an_unknown_original() -> None:
    with pytest.raises(ValueError, match="no reference of"):
        attach_embeddings(fp.from_json(document()))
