"""Storing sketches beside PDQ hashes: the hash path, the reuse path and the constraints (0037)."""

from collections.abc import Callable

import pytest
from django.db import IntegrityError, transaction

from catalog.models import Creator
from index.models import FrameHash, Media, Page
from matching import media as queue
from matching import media_hash
from matching.embedding import EMBEDDING_PIPELINE, SKETCH_BYTES
from matching.models import MediaReuse, Reference, ReferenceFrameHash
from matching.pdq import HashedFrame
from matching.tests.images import synthetic_image, to_bytes
from matching.tests.sketches import FakeEncoder, random_sketches

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake_encoder(monkeypatch: pytest.MonkeyPatch) -> FakeEncoder:
    """No model, no onnxruntime: the pipeline gets a sketch function of the right shape."""
    encoder = FakeEncoder()
    monkeypatch.setattr(media_hash, "embedder", lambda path=None: encoder.sketch)
    return encoder


def test_hashing_without_embeddings_stores_no_sketch_and_no_tag() -> None:
    hashed = media_hash.hash_media_bytes("poster", to_bytes(synthetic_image(1)))
    assert hashed.embedding_pipeline == ""
    assert [frame.embedding for frame in hashed.frames] == [None]


def test_hashing_with_embeddings_carries_one_sketch_per_frame(fake_encoder: FakeEncoder) -> None:
    hashed = media_hash.hash_media_bytes("poster", to_bytes(synthetic_image(2)), embed=True)
    assert hashed.embedding_pipeline == EMBEDDING_PIPELINE
    assert len(hashed.frames) == 1
    assert hashed.frames[0].embedding is not None
    assert len(hashed.frames[0].embedding) == SKETCH_BYTES
    assert fake_encoder.calls == 1


def test_the_pdq_hash_is_bit_identical_with_and_without_the_second_signal(
    fake_encoder: FakeEncoder,
) -> None:
    """Adding the embedding may not disturb the signal we already rely on."""
    data = to_bytes(synthetic_image(3))
    plain = media_hash.hash_media_bytes("poster", data)
    embedded = media_hash.hash_media_bytes("poster", data, embed=True)
    assert [frame.hash for frame in plain.frames] == [frame.hash for frame in embedded.frames]
    assert [frame.quality for frame in plain.frames] == [frame.quality for frame in embedded.frames]


def test_store_hashes_writes_the_sketch_and_its_tag(
    make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    page = make_page("a")
    media = make_media(page)
    sketch = random_sketches(1, seed=5)[0]
    queue.store_hashes(
        media,
        [HashedFrame(0, bytes(32), 90, sketch), HashedFrame(1, bytes([1]) + bytes(31), 90, None)],
        sha256="a" * 64,
        embedding_pipeline=EMBEDDING_PIPELINE,
    )
    rows = {
        row.frame_index: row
        for row in FrameHash.objects.filter(media=media).order_by("frame_index")
    }
    assert bytes(rows[0].embedding or b"") == sketch
    assert rows[0].embedding_pipeline == EMBEDDING_PIPELINE
    # A frame without a sketch stores no tag either: the pair is all or nothing.
    assert rows[1].embedding is None
    assert rows[1].embedding_pipeline == ""


def test_a_sketch_without_its_recipe_is_refused_by_the_database(
    make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    media = make_media(make_page("b"))
    with transaction.atomic(), pytest.raises(IntegrityError):
        FrameHash.objects.create(
            media=media,
            frame_index=0,
            pdq=bytes(32),
            quality=90,
            embedding=random_sketches(1, seed=6)[0],
            embedding_pipeline="",
        )


def test_a_sketch_of_the_wrong_length_is_refused_by_the_database(
    make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    media = make_media(make_page("c"))
    with transaction.atomic(), pytest.raises(IntegrityError):
        FrameHash.objects.create(
            media=media,
            frame_index=0,
            pdq=bytes(32),
            quality=90,
            embedding=bytes(32),
            embedding_pipeline=EMBEDDING_PIPELINE,
        )


def test_reused_hashes_carry_the_source_sketch(
    make_page: Callable[[str], Page], make_media: Callable[..., Media]
) -> None:
    """Identical bytes through the same decoder are the same picture, so the same sketch."""
    source_page, target_page = make_page("src"), make_page("dst")
    source = make_media(source_page, url="https://cdn.example.test/one.png")
    target = make_media(target_page, url="https://cdn.example.test/two.png")
    sketch = random_sketches(1, seed=7)[0]
    queue.store_hashes(
        source,
        [HashedFrame(0, bytes(32), 90, sketch)],
        sha256="b" * 64,
        embedding_pipeline=EMBEDDING_PIPELINE,
    )
    assert queue.reuse_hashes(
        target,
        source,
        reason=MediaReuse.Reason.SHA256,
        key="b" * 64,
        sha256="b" * 64,
        fetched=True,
    )
    row = FrameHash.objects.get(media=target)
    assert bytes(row.embedding or b"") == sketch
    assert row.embedding_pipeline == EMBEDDING_PIPELINE


def test_a_reference_sketch_only_sits_on_the_unflipped_orientation() -> None:
    creator = Creator.objects.create(name="C")
    reference = Reference.objects.create(
        creator=creator,
        title="t",
        sha256="c" * 64,
        frame_fps=1.0,
        duration_s=2.0,
        embedding_pipeline=EMBEDDING_PIPELINE,
    )
    sketch = random_sketches(1, seed=8)[0]
    ReferenceFrameHash.objects.create(
        reference=reference,
        frame_index=0,
        timestamp_s=0.0,
        pdq=bytes(32),
        quality=90,
        dihedral=0,
        embedding=sketch,
    )
    with transaction.atomic(), pytest.raises(IntegrityError):
        ReferenceFrameHash.objects.create(
            reference=reference,
            frame_index=0,
            timestamp_s=0.0,
            pdq=bytes(32),
            quality=90,
            dihedral=3,
            embedding=sketch,
        )
