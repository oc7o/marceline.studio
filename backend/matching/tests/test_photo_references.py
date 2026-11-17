"""Photo-collection references (issue #69, ADR 0020).

Synthetic images only (Pillow + numpy), never real media: `matching.tests.images` draws
gradients, shapes and text. The matching tests use random PDQ-shaped hashes instead of images,
so they measure the evidence rule and not the similarity of two drawings.
"""

import copy
import hashlib
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction

from catalog.models import Creator, Site
from index.models import FrameHash, Media, Page
from matching.fingerprint import (
    KIND_PHOTOS,
    PHOTO_PIPELINE,
    PIPELINE,
    FingerprintFormatError,
    collection_digest,
    collection_sha256,
    fingerprint_photos,
    from_json,
    to_json,
)
from matching.models import Reference, ReferenceMatch
from matching.references import STRICT_MAX_DISTANCE, match_references
from matching.tests.hashes import flip_bits, random_hashes
from matching.tests.images import synthetic_image, to_bytes
from matching.tests.test_fingerprint import load_client
from matching.timeline import COLLECTION_REASON

# Four unrelated pictures of one set, as the matcher sees them: far apart, no order.
IMAGES = random_hashes(4, seed=69)
# A distance that is loose but not strict: evidence only with a timeline or
# corroboration. Derived, so it moves with the threshold instead of breaking.
LOOSE_ONLY = STRICT_MAX_DISTANCE + 2


@pytest.fixture(scope="module")
def folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A photo set on disk, in every format the server's Pillow decodes."""
    path = tmp_path_factory.mktemp("originals") / "Beach Set"
    path.mkdir()
    for index, (name, fmt) in enumerate(
        [("01.png", "PNG"), ("02.jpg", "JPEG"), ("03.webp", "WEBP"), ("04.avif", "AVIF")]
    ):
        (path / name).write_bytes(to_bytes(synthetic_image(index + 40), fmt))
    return path


@pytest.fixture(scope="module")
def document(folder: Path) -> dict[str, Any]:
    return to_json(fingerprint_photos(images_in(folder)))


@pytest.fixture
def creator(db: None) -> Creator:
    return Creator.objects.create(name="Photo Creator", aliases=["Alias"])


@pytest.fixture
def collection(creator: Creator) -> Reference:
    """An imported collection, straight from hashes: four images, no fps, no duration."""
    reference = Reference.objects.create(
        creator=creator,
        kind=Reference.Kind.PHOTOS,
        title="Beach Set",
        sha256=hashlib.sha256(b"beach-set").hexdigest(),
    )
    for index, value in enumerate(IMAGES):
        reference.frames.create(frame_index=index, timestamp_s=float(index), pdq=value, quality=100)
    return reference


def images_in(folder: Path) -> list[Path]:
    return sorted(path for path in folder.iterdir() if path.is_file())


def run(*args: str) -> str:
    output = io.StringIO()
    try:
        call_command("import_fingerprints", *args, stdout=output)
    except CommandError as exc:
        raise CommandError(f"{exc}\n{output.getvalue()}") from exc
    return output.getvalue()


def hover_page(site: Site, slug: str, values: list[bytes]) -> Page:
    page = Page.objects.create(site=site, title=slug, url=f"https://{site.domain}/{slug}")
    for index, value in enumerate(values, start=1):
        media = Media.objects.create(
            page=page, kind="frame", url=f"https://cdn.test/imgs/1/2/{slug}_{index}.jpg"
        )
        FrameHash.objects.create(media=media, pdq=value, quality=100)
    return page


def poster_page(site: Site, slug: str, value: bytes) -> Page:
    page = Page.objects.create(site=site, title=slug, url=f"https://{site.domain}/{slug}")
    media = Media.objects.create(page=page, kind="poster", url=f"https://cdn.test/{slug}.jpg")
    FrameHash.objects.create(media=media, pdq=value, quality=100)
    return page


def test_fingerprint_photos(folder: Path) -> None:
    paths = images_in(folder)
    collection = fingerprint_photos(paths, creators=["Alias"])
    assert (collection.kind, collection.pipeline) == (KIND_PHOTOS, PHOTO_PIPELINE)
    # The title is the folder's name, never its path.
    assert collection.title == "Beach Set"
    assert (collection.image_count, collection.frame_count, collection.unit) == (4, 4, "images")
    assert len(collection.frames) == 4 * 8
    assert (collection.fps, collection.duration_s) == (None, None)
    # Positions are ordinals in sorted order; the timestamp only repeats them.
    assert sorted({frame.frame_index for frame in collection.frames}) == [0, 1, 2, 3]
    assert all(frame.timestamp_s == float(frame.frame_index) for frame in collection.frames)
    assert set(collection.client) == {"tool", "system", "machine", "python", "pillow", "pdqhash"}
    # The identity is a digest of the images' digests: order-independent, and the same whether
    # it is taken from the files or from the run that hashed them.
    assert collection_digest(list(reversed(paths))) == collection.sha256
    assert fingerprint_photos(list(reversed(paths))).sha256 == collection.sha256
    assert collection_sha256(["0" * 64]) != "0" * 64
    text = json.dumps(to_json(collection))
    assert str(folder) not in text and "timestamp_s" not in text
    assert from_json(json.loads(text)) == collection


def test_no_images_and_too_many(folder: Path) -> None:
    with pytest.raises(ValueError, match="at least one image"):
        fingerprint_photos([])
    with pytest.raises(ValueError, match="at most"):
        fingerprint_photos(images_in(folder) * 5001)


PHOTO_TAMPERING: dict[str, Callable[[dict[str, Any]], None]] = {
    "video pipeline on a collection": lambda d: d.update(pipeline=PIPELINE),
    "unknown pipeline": lambda d: d.update(pipeline="scanner-v1"),
    "timestamp on an image": lambda d: d["frames"][0].update(timestamp_s=0.0),
    "duration on a collection": lambda d: d.update(duration_s=10.0),
    "missing image_count": lambda d: d.pop("image_count"),
    "image_count zero": lambda d: d.update(image_count=0),
    "ordinal beyond the collection": lambda d: d["frames"][0].update(frame_index=99),
    "declared as a video": lambda d: d.update(kind="video"),
    "unknown kind": lambda d: d.update(kind="gallery"),
    "missing orientation": lambda d: d["frames"].pop(3),
    "path leaked in": lambda d: d.update(folder="/Users/someone/Beach Set"),
}


@pytest.mark.parametrize("name", PHOTO_TAMPERING)
def test_photo_documents_stay_strict(name: str, document: dict[str, Any]) -> None:
    data = copy.deepcopy(document)
    PHOTO_TAMPERING[name](data)
    with pytest.raises(FingerprintFormatError):
        from_json(data)


@pytest.mark.django_db
def test_import_collection(folder: Path, tmp_path: Path, creator: Creator) -> None:
    partner = Creator.objects.create(name="Carla Bremer")
    data = to_json(fingerprint_photos(images_in(folder), creators=["Alias", "Carla Bremer"]))
    path = tmp_path / "beach-set.json"
    path.write_text(json.dumps(data))

    dry = run("--dry-run", str(path))
    assert "would import for Photo Creator + Carla Bremer: 4 images, 32 hashes" in dry
    assert not Reference.objects.exists()

    output = run(str(path))
    reference = Reference.objects.get()
    assert f"imported reference {reference.pk}: 4 images, 32 hashes" in output
    assert reference.kind == Reference.Kind.PHOTOS
    assert (reference.frame_fps, reference.duration_s) == (None, None)
    assert (reference.pipeline, reference.title) == (PHOTO_PIPELINE, "Beach Set")
    assert reference.sha256 == data["sha256"]
    assert reference.client["source"] == "local-cli"
    assert reference.frames.count() == 32
    assert sorted(set(reference.frames.values_list("frame_index", flat=True))) == [0, 1, 2, 3]
    assert all(row.timestamp_s == row.frame_index for row in reference.frames.all())
    # Several creators per collection (issue #64): the first name is the rights holder.
    assert reference.creator == creator
    assert set(reference.creators.values_list("pk", flat=True)) == {creator.pk, partner.pk}
    # The collection digest is the identity: the same set is refused a second time.
    assert "skipped (already imported" in run(str(path))
    assert Reference.objects.count() == 1


@pytest.mark.django_db
def test_shape_is_a_database_constraint(creator: Creator) -> None:
    """A collection can never claim a timeline, and a video can never lose its sampling rate."""
    impossible: tuple[dict[str, Any], ...] = (
        {"kind": Reference.Kind.PHOTOS, "frame_fps": 1.0},
        {"kind": Reference.Kind.PHOTOS, "duration_s": 600.0},
        {"kind": Reference.Kind.VIDEO, "frame_fps": None},
        {"kind": Reference.Kind.VIDEO, "frame_fps": 0.0},
    )
    for index, fields in enumerate(impossible):
        with transaction.atomic(), pytest.raises(IntegrityError):
            Reference.objects.create(creator=creator, title="bad", sha256=str(index) * 64, **fields)
    Reference.objects.create(
        creator=creator, kind=Reference.Kind.PHOTOS, title="a set", sha256="a" * 64
    )
    Reference.objects.create(
        creator=creator, kind=Reference.Kind.VIDEO, title="a video", sha256="b" * 64, frame_fps=1.0
    )


@pytest.mark.django_db
def test_matching_has_no_timeline_and_counts_images(collection: Reference, site: Site) -> None:
    """The evidence rule of ADR 0020, on one run.

    `spread` reposts three photos of the set in an order unrelated to the collection's own: a
    video would be scored on that order, a collection must not be. `repeat` shows the same photo
    twice, `single` shows one photo.
    """
    spread = hover_page(
        site,
        "gallery",
        [flip_bits(IMAGES[2], 6), flip_bits(IMAGES[0], 6), flip_bits(IMAGES[1], 6)],
    )
    single = poster_page(site, "single", flip_bits(IMAGES[3], 6))
    repeat = hover_page(site, "repeat", [flip_bits(IMAGES[0], 6), flip_bits(IMAGES[0], 9)])
    match_references([collection])

    many = ReferenceMatch.objects.get(page=spread)
    assert many.timeline_score is None and many.consistent_frames == 0
    assert many.timeline_reason == COLLECTION_REASON
    assert many.matched_seconds == [0, 1, 2]
    assert many.confidence_signals["collection_images"] == 3
    assert (many.evidence_frames, many.strength, many.band) == (3, "strong", "high")

    one = ReferenceMatch.objects.get(page=single)
    assert one.confidence_signals["collection_images"] == 1
    # One matched image earns nothing from the collection component: it is the weakest evidence
    # there is, and it never reaches the high band.
    assert one.confidence_signals["images"] == 0.0
    assert (one.evidence_frames, one.strength, one.band) == (1, "weak", "review")

    same = ReferenceMatch.objects.get(page=repeat)
    assert same.evidence_frames == 2
    assert same.matched_seconds == [0]
    assert same.confidence_signals["collection_images"] == 1
    assert (same.strength, same.band) == ("weak", "review")
    assert one.confidence < same.confidence < many.confidence


@pytest.mark.django_db
def test_loose_frames_need_corroboration(collection: Reference, site: Site) -> None:
    """Without an order there is no chain to admit a loose-only hash; nothing else changes."""
    far = poster_page(site, "far", flip_bits(IMAGES[0], LOOSE_ONLY))
    match_references([collection])
    assert not ReferenceMatch.objects.filter(page=far).exists()


def test_client_writes_one_file_per_collection(
    folder: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "fingerprints"
    client = load_client()
    argv = ["--photos", "--jobs", "1", "--out", str(out), "--creator", "Alias", str(folder)]

    assert client.main(argv) == 0
    [path] = list(out.iterdir())
    fingerprint = from_json(json.loads(path.read_text()))
    assert path.name == f"Beach_Set.{fingerprint.sha256[:12]}.json"
    assert (fingerprint.kind, fingerprint.image_count) == (KIND_PHOTOS, 4)
    assert (fingerprint.title, fingerprint.creator) == ("Beach Set", "Alias")
    assert str(folder) not in path.read_text()
    assert str(tmp_path) not in path.read_text()
    errors = capsys.readouterr().err
    assert "done Beach Set: 4 of 4 images hashed" in errors
    assert "import_fingerprints" in errors

    # A second run writes nothing: the collection digest already has a file.
    assert client.main(argv) == 0
    assert list(out.iterdir()) == [path]
    assert "skipped Beach Set" in capsys.readouterr().err

    # A folder without images is reported, not silently skipped.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert client.main(["--photos", "--out", str(out), str(empty)]) == 1
    assert "no images there" in capsys.readouterr().err
