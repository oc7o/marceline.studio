import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction

from catalog.models import Creator
from matching import fingerprint as fingerprint_module
from matching.fingerprint import (
    BROWSER_PHOTO_PIPELINE,
    BROWSER_PIPELINE,
    BROWSER_PIPELINE_V0,
    CROP_GRID_PIPELINE,
    FORMAT_VERSION,
    KIND_PHOTOS,
    KIND_VIDEO,
    PHOTO_PIPELINE,
    PIPELINE,
    PIPELINE_V0,
    PIPELINES,
    PIPELINES_BY_KIND,
    Fingerprint,
    FingerprintFormatError,
    FingerprintFrame,
    compare_frames,
    file_sha256,
    fingerprint_video,
    from_json,
    pipeline_label,
    to_json,
)
from matching.frames import iter_video_frames
from matching.models import Reference
from matching.references import add_reference, import_fingerprint
from matching.tests.test_frames import make_video

BACKEND = Path(__file__).resolve().parents[2]
CLIENT = BACKEND.parent / "client" / "fingerprint.py"


@pytest.fixture
def original(tmp_path: Path) -> Path:
    make_video(tmp_path, "testsrc2=duration=3:size=320x240:rate=10")
    return tmp_path / "clip.mp4"


@pytest.fixture
def creator(db: None) -> Creator:
    return Creator.objects.create(name="Synthetic Creator", aliases=["Alias"])


def synthetic(
    sha256: str = "ab" * 32, creators: tuple[str, ...] = ("Alias",), dihedral: bool = True
) -> Fingerprint:
    orientations = range(8 if dihedral else 1)
    frames = tuple(
        FingerprintFrame(index, index / 2, orientation, 90, bytes([index * 8 + orientation]) * 32)
        for index in range(3)
        for orientation in orientations
    )
    return Fingerprint(
        sha256=sha256,
        file_size=1000,
        duration_s=1.5,
        fps=2.0,
        dihedral=dihedral,
        pipeline=PIPELINE,
        title="Synthetic original",
        creators=creators,
        client={"tool": "test", "system": "Darwin"},
        frames=frames,
    )


def synthetic_photos(sha256: str = "cd" * 32, pipeline: str = PHOTO_PIPELINE) -> Fingerprint:
    """A photo collection: ordinals, no duration, no rate, no timestamp (ADR 0020)."""
    frames = tuple(
        FingerprintFrame(
            index, float(index), orientation, 90, bytes([index * 8 + orientation]) * 32
        )
        for index in range(3)
        for orientation in range(8)
    )
    return Fingerprint(
        kind=KIND_PHOTOS,
        sha256=sha256,
        file_size=1000,
        duration_s=None,
        fps=None,
        dihedral=True,
        pipeline=pipeline,
        title="Synthetic set",
        creators=("Alias",),
        client={"tool": "test", "pillow": "12.3"},
        frames=frames,
        image_count=3,
    )


def write(path: Path, fingerprint: Fingerprint) -> Path:
    path.write_text(json.dumps(to_json(fingerprint)))
    return path


def run(*args: str) -> str:
    output = io.StringIO()
    try:
        call_command("import_fingerprints", *args, stdout=output)
    except CommandError as exc:
        raise CommandError(f"{exc}\n{output.getvalue()}") from exc
    return output.getvalue()


@pytest.mark.django_db
def test_fingerprint_matches_add_reference(original: Path, creator: Creator) -> None:
    fingerprint = fingerprint_video(original, fps=2, title="Original", creators=["Alias"])
    reference = add_reference(creator=creator, path=original, fps=2, title="Original")
    stored = sorted(
        (index, orientation, bytes(pdq), quality, timestamp)
        for index, orientation, pdq, quality, timestamp in reference.frames.values_list(
            "frame_index", "dihedral", "pdq", "quality", "timestamp_s"
        )
    )
    computed = sorted(
        (frame.frame_index, frame.dihedral, frame.pdq, frame.quality, frame.timestamp_s)
        for frame in fingerprint.frames
    )
    assert computed == stored
    assert len(computed) == 6 * 8
    assert fingerprint.sha256 == reference.sha256
    assert fingerprint.file_size == original.stat().st_size
    assert fingerprint.duration_s == pytest.approx(reference.duration_s)
    assert fingerprint.frame_count == 6
    assert reference.pipeline == PIPELINE
    assert reference.client["source"] == "server"
    assert set(fingerprint.client) == {"tool", "system", "machine", "python", "ffmpeg", "pdqhash"}
    assert str(original.parent) not in json.dumps(to_json(fingerprint))


def test_fingerprint_checks_digest_and_options(original: Path) -> None:
    with pytest.raises(ValueError, match="changed"):
        fingerprint_video(original, sha256="0" * 64)
    with pytest.raises(ValueError, match="fps"):
        fingerprint_video(original, fps=float("nan"))
    with pytest.raises(ValueError, match="timeout"):
        next(iter(iter_video_frames(original, fps=1, timeout_s=0)))
    plain = fingerprint_video(original, dihedral=False)
    assert plain.title == "clip"
    assert {frame.dihedral for frame in plain.frames} == {0}
    assert from_json(json.loads(json.dumps(to_json(plain)))) == plain


def test_json_round_trip() -> None:
    for fingerprint in (synthetic(), synthetic(creators=(), dihedral=False)):
        assert from_json(json.loads(json.dumps(to_json(fingerprint)))) == fingerprint


def test_current_version_creator_list_round_trip() -> None:
    """`to_json` always writes the current version; v5 added the crop lattice (ADR 0039)."""
    fingerprint = synthetic(creators=("Sasha Meyer", "Carla Bremer"))
    data = to_json(fingerprint)
    assert (data["version"], data["kind"]) == (FORMAT_VERSION, "video")
    assert (FORMAT_VERSION, data["embedding_pipeline"], data["crop_grid"]) == (5, "", None)
    assert data["creator"] == ["Sasha Meyer", "Carla Bremer"]
    assert from_json(json.loads(json.dumps(data))) == fingerprint
    # The primary creator is the first name; a file may still name nobody.
    assert fingerprint.creator == "Sasha Meyer"
    assert synthetic(creators=()).creator is None
    assert to_json(synthetic(creators=()))["creator"] == []


def test_older_versions_still_import() -> None:
    """Files written before issue #64 store one name or null under `creator` and no `kind`."""
    data = to_json(synthetic())
    # A version 1 document has no `kind` (v3), no `embedding_pipeline` (v4) and no `crop_grid` (v5).
    data.pop("kind")
    data.pop("embedding_pipeline")
    data.pop("crop_grid")
    data["version"], data["creator"] = 1, "Alias"
    assert from_json(data).creators == ("Alias",)
    assert from_json(data).kind == "video"
    data["creator"] = None
    assert from_json(data).creators == ()
    # A list is version 2 only; a version 1 file carrying one is malformed.
    data["creator"] = ["Alias"]
    with pytest.raises(FingerprintFormatError):
        from_json(data)


def beyond_duration(data: dict[str, Any]) -> None:
    for frame in data["frames"][16:]:
        frame["frame_index"], frame["timestamp_s"] = 10, 5.0


TAMPERING: dict[str, Callable[[dict[str, Any]], None]] = {
    "short hex": lambda d: d["frames"][0].update(pdq=d["frames"][0]["pdq"][:62]),
    "uppercase hex": lambda d: d["frames"][11].update(pdq=d["frames"][11]["pdq"].upper()),
    "missing orientation": lambda d: d["frames"].pop(3),
    "orientation 8": lambda d: d["frames"][7].update(dihedral=8),
    "orientations without dihedral": lambda d: d.update(dihedral=False),
    "duplicate orientation": lambda d: d["frames"].append(dict(d["frames"][0])),
    "bad timestamp": lambda d: d["frames"][8].update(timestamp_s=0.6),
    "unknown format": lambda d: d.update(format="something-else"),
    # 4 is the current version; the next one does not exist yet and must not be guessed at.
    "unsupported version": lambda d: d.update(version=FORMAT_VERSION + 1),
    "unknown kind": lambda d: d.update(kind="audio"),
    "kind missing since v3": lambda d: d.pop("kind"),
    "embedding recipe missing since v4": lambda d: d.pop("embedding_pipeline"),
    "photo pipeline on a video": lambda d: d.update(pipeline=PHOTO_PIPELINE),
    "image_count on a video": lambda d: d.update(image_count=3),
    "creator not a list": lambda d: d.update(creator="Alias"),
    "creator entry not a string": lambda d: d.update(creator=[1]),
    "blank creator": lambda d: d.update(creator=["  "]),
    "duplicate creator": lambda d: d.update(creator=["Alias", "alias"]),
    "too many creators": lambda d: d.update(creator=[f"Creator {i}" for i in range(21)]),
    "unknown pipeline": lambda d: d.update(pipeline="browser-v1"),
    "beyond duration": beyond_duration,
    "short sha256": lambda d: d.update(sha256="ab" * 31),
    "zero size": lambda d: d.update(file_size=0),
    "nan fps": lambda d: d.update(fps=float("nan")),
    "low quality": lambda d: d["frames"][0].update(quality=49),
    "boolean index": lambda d: d["frames"][0].update(frame_index=True),
    "empty title": lambda d: d.update(title=" "),
    "long title": lambda d: d.update(title="x" * 256),
    "extra key": lambda d: d.update(path="/Users/someone/video.mp4"),
    "client host": lambda d: d.update(client={"host name": "x"}),
}


@pytest.mark.parametrize("name", TAMPERING)
def test_from_json_rejects_tampering(name: str) -> None:
    data = to_json(synthetic())
    TAMPERING[name](data)
    with pytest.raises(FingerprintFormatError):
        from_json(data)


def test_browser_pipeline_is_accepted_but_distinct() -> None:
    """The browser hashes the same video through a different decoder, scaler and colour matrix.

    Measured in real headless Chromium over synthetic clips (ADR 0019, re-run for ADR 0022 once
    the sampling grids agreed): the same decoded picture hashes a median of 2 bits away, but 20
    bits (max 30 as stored) at 1280x720, where the canvas downscale does the most work. The match
    threshold is 31. Too close to pretend the recipes are the same, so the tags must stay
    different and all of them must be importable.
    """
    assert BROWSER_PIPELINE != PIPELINE
    assert {
        PIPELINE,
        PIPELINE_V0,
        BROWSER_PIPELINE,
        BROWSER_PIPELINE_V0,
        CROP_GRID_PIPELINE,
        PHOTO_PIPELINE,
        BROWSER_PHOTO_PIPELINE,
    } == PIPELINES
    browser = to_json(synthetic())
    browser["pipeline"] = BROWSER_PIPELINE
    assert from_json(browser).pipeline == BROWSER_PIPELINE
    # The frame-size fix (ffmpeg's height rounding) is a new browser tag; the old one still reads.
    assert (BROWSER_PIPELINE, BROWSER_PIPELINE_V0) == (
        "browser-rgb512-pdq-v1",
        "browser-rgb512-pdq-v0",
    )
    browser["pipeline"] = BROWSER_PIPELINE_V0
    assert from_json(browser).pipeline == BROWSER_PIPELINE_V0
    assert "superseded frame size" in pipeline_label(BROWSER_PIPELINE_V0)
    # An unknown recipe is still refused: a tag is a promise about how the bits were made.
    browser["pipeline"] = "canvas-v9"
    with pytest.raises(FingerprintFormatError):
        from_json(browser)


def test_the_browser_photo_recipe_is_a_photo_tag_of_its_own() -> None:
    """Issue #116 / ADR 0053: a browser decodes an image, Pillow decodes an image, and nobody has
    measured how far apart the two are. A tag is what records that, so the two photo recipes are
    different tags, both are photo recipes, and neither is accepted on a video document."""
    assert BROWSER_PHOTO_PIPELINE == "browser-rgb-pdq-v0"
    assert {PHOTO_PIPELINE, BROWSER_PHOTO_PIPELINE} == PIPELINES_BY_KIND[KIND_PHOTOS]
    assert BROWSER_PHOTO_PIPELINE not in PIPELINES_BY_KIND[KIND_VIDEO]
    assert "platform image decoder" in pipeline_label(BROWSER_PHOTO_PIPELINE)

    document = to_json(synthetic_photos())
    document["pipeline"] = BROWSER_PHOTO_PIPELINE
    collection = from_json(document)
    assert (collection.kind, collection.pipeline) == (KIND_PHOTOS, BROWSER_PHOTO_PIPELINE)
    # A photo tag on a video document, and a video tag on a photo document, stay refused.
    video = to_json(synthetic())
    video["pipeline"] = BROWSER_PHOTO_PIPELINE
    with pytest.raises(FingerprintFormatError):
        from_json(video)
    document["pipeline"] = BROWSER_PIPELINE
    with pytest.raises(FingerprintFormatError):
        from_json(document)


def test_grid_fix_is_a_new_tag_and_the_old_one_still_reads() -> None:
    """Issue #70 / ADR 0022: v1 samples the picture at k, v0 sampled it ~0.47 s late.

    The old tag must keep meaning exactly what it meant, so a v0 document still validates and
    still imports; only a re-run over the source file produces v1. An unknown tag stays refused.
    """
    assert (PIPELINE, PIPELINE_V0) == ("ffmpeg-rgb512-pdq-v1", "ffmpeg-rgb512-pdq-v0")
    assert {
        PIPELINE,
        PIPELINE_V0,
        BROWSER_PIPELINE,
        BROWSER_PIPELINE_V0,
        CROP_GRID_PIPELINE,
        PHOTO_PIPELINE,
        BROWSER_PHOTO_PIPELINE,
    } == PIPELINES
    old = to_json(synthetic())
    old["pipeline"] = PIPELINE_V0
    assert from_json(old).pipeline == PIPELINE_V0
    old["pipeline"] = "ffmpeg-rgb512-pdq-v2"
    with pytest.raises(FingerprintFormatError):
        from_json(old)
    # The operator has to be able to see which grid a row is on, in words.
    assert pipeline_label(PIPELINE).startswith(f"{PIPELINE} (")
    assert "superseded" in pipeline_label(PIPELINE_V0)
    assert pipeline_label("who-knows-v9") == "who-knows-v9"


@pytest.mark.django_db
def test_v1_imports_beside_a_v0_reference_of_the_same_file(
    tmp_path: Path, creator: Creator
) -> None:
    """Re-fingerprinting a library: the new hashes land next to the old row, nothing is deleted."""
    write(tmp_path / "new.json", synthetic())
    old = to_json(synthetic())
    old["pipeline"] = PIPELINE_V0
    (tmp_path / "old.json").write_text(json.dumps(old))

    run(str(tmp_path / "old.json"))
    v0 = Reference.objects.get()
    assert v0.pipeline == PIPELINE_V0

    # --compare measures the grid gap against the old row instead of importing.
    comparison = run("--compare", str(tmp_path / "new.json"))
    assert f"pipeline differs: stored {PIPELINE_V0}, file {PIPELINE}" in comparison
    assert Reference.objects.count() == 1

    output = run(str(tmp_path / "new.json"))
    v1 = Reference.objects.exclude(pk=v0.pk).get()
    assert v1.pipeline == PIPELINE
    assert f"alongside reference {v0.pk} on {PIPELINE_V0}" in output
    assert v0.sha256 == v1.sha256
    # The same file on the *same* pipeline is still only stored once.
    assert "skipped (already imported" in run(str(tmp_path / "new.json"))
    assert Reference.objects.count() == 2


@pytest.mark.django_db
def test_a_re_fingerprinted_file_inherits_the_cast_of_its_earlier_reference(
    tmp_path: Path, creator: Creator
) -> None:
    """Bulk re-runs after a pipeline change need no --creator (issue #70).

    The operator re-fingerprints a whole library at once, so the client is not given a cast per
    file. The sha256 is unchanged by a pipeline change, so a reference with that digest is *the
    same video* and its cast is authoritative. Inheriting must never be silent.
    """
    old = to_json(synthetic())
    old["pipeline"] = PIPELINE_V0
    (tmp_path / "old.json").write_text(json.dumps(old))
    run(str(tmp_path / "old.json"))
    v0 = Reference.objects.get()

    # The re-run names nobody we know: the placeholder the operator left in the command.
    write(tmp_path / "new.json", synthetic(creators=("NAME",)))
    output = run(str(tmp_path / "new.json"))

    v1 = Reference.objects.exclude(pk=v0.pk).get()
    assert v1.creator == creator == v0.creator
    assert f"cast from reference {v0.pk}" in output


@pytest.mark.django_db
def test_an_explicit_creator_still_wins_over_the_inherited_cast(
    tmp_path: Path, creator: Creator
) -> None:
    other = Creator.objects.create(name="Second Creator")
    old = to_json(synthetic())
    old["pipeline"] = PIPELINE_V0
    (tmp_path / "old.json").write_text(json.dumps(old))
    run(str(tmp_path / "old.json"))

    write(tmp_path / "new.json", synthetic(creators=("NAME",)))
    output = run("--creator", other.name, str(tmp_path / "new.json"))

    assert Reference.objects.get(pipeline=PIPELINE).creator == other
    assert "cast from reference" not in output


@pytest.mark.django_db
def test_an_unknown_cast_with_no_earlier_reference_still_fails(tmp_path: Path) -> None:
    """Inheritance is a fallback for a known file, never a way to import an unattributed one."""
    write(tmp_path / "new.json", synthetic(creators=("NAME",)))

    with pytest.raises(CommandError):
        run(str(tmp_path / "new.json"))
    assert not Reference.objects.exists()


def test_compare_frames() -> None:
    rows = [(frame.frame_index, frame.dihedral, frame.pdq) for frame in synthetic().frames]
    same = compare_frames(rows, rows)
    assert (same.both, same.only_first, same.only_second) == (24, 0, 0)
    assert (same.identical_share, same.max) == (1.0, 0)
    changed = [(0, 0, bytes([255]) * 32), *rows[1:], (9, 0, bytes(32))]
    parity = compare_frames(changed, rows[:-1])
    assert (parity.both, parity.only_first, parity.only_second) == (23, 2, 0)
    assert parity.max == 256 and parity.p50 == 0
    assert compare_frames([], rows).max == 0


@pytest.mark.django_db
def test_import_fingerprints(tmp_path: Path, creator: Creator) -> None:
    write(tmp_path / "one.json", synthetic())
    output = run("--dry-run", str(tmp_path))
    assert "would import for Synthetic Creator: 3 frames, 24 hashes" in output
    assert not Reference.objects.exists()

    output = run("--source-note", "Synthetic test", str(tmp_path))
    reference = Reference.objects.get()
    assert f"one.json: imported reference {reference.pk}: 3 frames, 24 hashes" in output
    assert "ab" * 16 not in output
    assert reference.creator == creator
    assert reference.title == "Synthetic original"
    assert reference.frame_fps == 2
    assert reference.source_note == "Synthetic test"
    assert reference.pipeline == PIPELINE
    assert reference.client == {"tool": "test", "system": "Darwin", "source": "local-cli"}
    assert reference.frames.count() == 24

    output = run(str(tmp_path / "one.json"))
    assert f"skipped (already imported as reference {reference.pk})" in output
    assert Reference.objects.count() == 1

    assert "distance 0 for 100.0%" in run("--compare", str(tmp_path / "one.json"))
    data = to_json(synthetic())
    data["frames"][0]["pdq"] = "f" * 64
    (tmp_path / "one.json").write_text(json.dumps(data))
    output = run("--compare", str(tmp_path / "one.json"))
    assert "24 pairs in both" in output
    match = re.search(r"max (\d+)", output)
    assert match is not None and int(match.group(1)) > 0
    assert reference.frames.count() == 24


@pytest.mark.django_db
def test_import_fingerprints_continues_after_errors(tmp_path: Path, creator: Creator) -> None:
    write(tmp_path / "a.json", synthetic("aa" * 32))
    (tmp_path / "b.json").write_text("{not json")
    write(tmp_path / "c.json", synthetic("cc" * 32, creators=()))
    write(tmp_path / "d.json", synthetic("dd" * 32, creators=("Nobody",)))
    write(tmp_path / "e.json", synthetic("ee" * 32))
    with pytest.raises(CommandError, match="3 fingerprint file") as failure:
        run(str(tmp_path))
    output = str(failure.value)
    assert "b.json: error" in output
    assert "c.json: error: no creator" in output
    assert "d.json: error: Unknown or ambiguous creator" in output
    assert set(Reference.objects.values_list("sha256", flat=True)) == {"aa" * 32, "ee" * 32}

    run("--creator", creator.name, str(tmp_path / "c.json"))
    assert Reference.objects.filter(sha256="cc" * 32, creator=creator).exists()


@pytest.mark.django_db
def test_import_fingerprints_several_creators(tmp_path: Path, creator: Creator) -> None:
    partner = Creator.objects.create(name="Carla Bremer")
    write(tmp_path / "pair.json", synthetic(creators=("Alias", "Carla Bremer")))
    assert "would import for Synthetic Creator + Carla Bremer" in run("--dry-run", str(tmp_path))
    run(str(tmp_path))
    reference = Reference.objects.get()
    assert reference.creator == creator
    assert set(reference.creators.values_list("pk", flat=True)) == {creator.pk, partner.pk}

    # --creator overrides the file entirely; its first name becomes the primary creator.
    write(tmp_path / "flip.json", synthetic("cd" * 32, creators=("Alias",)))
    run("--creator", partner.name, "--creator", creator.name, str(tmp_path / "flip.json"))
    flipped = Reference.objects.get(sha256="cd" * 32)
    assert flipped.creator == partner
    assert set(flipped.creators.values_list("pk", flat=True)) == {creator.pk, partner.pk}

    # An unknown co-performer fails that file rather than importing a partial cast.
    write(tmp_path / "bad.json", synthetic("ef" * 32, creators=("Alias", "Nobody")))
    with pytest.raises(CommandError, match="1 fingerprint file"):
        run(str(tmp_path / "bad.json"))
    assert not Reference.objects.filter(sha256="ef" * 32).exists()


def load_client() -> ModuleType:
    spec = importlib.util.spec_from_file_location("marceline_fingerprint_client", CLIENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_client_fetches_the_model_without_being_given_a_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--fetch-model` is a job of its own (ADR 0037): it must not demand a PATH it never reads.

    The operator runs this once locally before re-fingerprinting anything, so requiring a
    dummy path would be a paste-and-fail. Nothing is downloaded here: the fetch is replaced.
    """
    client = load_client()
    calls: list[int] = []

    def fetch() -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(client, "fetch_model", fetch)

    assert client.main(["--fetch-model"]) == 0
    assert calls == [1]

    # Without a path and without that flag there is nothing to do, and argparse says so.
    with pytest.raises(SystemExit):
        client.main([])
    assert "at least one PATH" in capsys.readouterr().err


def test_the_client_offers_the_embedding_flags(tmp_path: Path) -> None:
    """Both flags exist and default to off, so a plain run is byte-identical to before."""
    client = load_client()
    args = client.parser().parse_args([str(tmp_path)])
    assert (args.embeddings, args.fetch_model) == (False, False)
    assert client.embedding_suffix(False) == ""
    assert client.embedding_suffix(True) == ".emb"
    assert client.sketch_function(False) is None


def test_client_script(tmp_path: Path, original: Path, capsys: pytest.CaptureFixture[str]) -> None:
    videos = tmp_path / "videos" / "nested"
    videos.mkdir(parents=True)
    original.rename(videos / "My Clip.MP4")
    (videos / "._My Clip.MP4").write_bytes(b"AppleDouble metadata")
    (videos / "notes.txt").write_text("not a video")
    out = tmp_path / "fingerprints"
    client = load_client()
    argv = ["--jobs", "1", "--out", str(out), "--creator", "Alias", str(tmp_path / "videos")]

    assert client.main(argv) == 0
    [path] = out.iterdir()
    fingerprint = from_json(json.loads(path.read_text()))
    assert path.name == f"My_Clip.{fingerprint.sha256[:12]}.json"
    assert fingerprint.title == "My Clip"
    assert fingerprint.creator == "Alias"
    assert fingerprint.frame_count == 3
    assert str(tmp_path) not in path.read_text()
    errors = capsys.readouterr().err
    assert "done My Clip.MP4: 3 frames" in errors
    assert "rsync -av" in errors and "import_fingerprints" in errors

    assert client.main(argv) == 0
    assert "skipped My Clip.MP4" in capsys.readouterr().err
    assert list(out.iterdir()) == [path]

    assert client.main(["--jobs", "1", "--out", str(out), str(tmp_path / "missing")]) == 1
    with pytest.raises(SystemExit):
        client.main(["--title", "x", str(tmp_path / "videos"), str(path)])


def test_client_script_repeatable_creator(
    tmp_path: Path, original: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "fingerprints"
    client = load_client()
    argv = [
        "--jobs",
        "1",
        "--out",
        str(out),
        "--creator",
        "Sasha Meyer",
        "--creator",
        "Carla Bremer",
        str(original),
    ]

    assert client.main(argv) == 0
    [path] = out.iterdir()
    fingerprint = from_json(json.loads(path.read_text()))
    assert fingerprint.creators == ("Sasha Meyer", "Carla Bremer")
    # The printed next step repeats --creator, so the import keeps the same cast and order.
    assert "--creator 'Sasha Meyer' --creator 'Carla Bremer'" in capsys.readouterr().err


def test_fingerprint_module_imports_no_django() -> None:
    code = (
        "import sys, matching.fingerprint\n"
        "roots = {name.split('.')[0] for name in sys.modules}\n"
        "assert not roots & {'django', 'catalog', 'index'}, roots\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=BACKEND, check=True, timeout=60)


def at_one_fps(fingerprint: Fingerprint) -> Fingerprint:
    """The same file sampled at 1 fps: every other moment of the 2 fps synthetic document."""
    frames = tuple(
        FingerprintFrame(
            int(frame.timestamp_s), frame.timestamp_s, frame.dihedral, frame.quality, frame.pdq
        )
        for frame in fingerprint.frames
        if frame.timestamp_s.is_integer()
    )
    return Fingerprint(
        sha256=fingerprint.sha256,
        file_size=fingerprint.file_size,
        duration_s=fingerprint.duration_s,
        fps=1.0,
        dihedral=fingerprint.dihedral,
        pipeline=fingerprint.pipeline,
        title=fingerprint.title,
        creators=fingerprint.creators,
        client=fingerprint.client,
        frames=frames,
    )


@pytest.mark.django_db
def test_a_denser_re_fingerprint_imports_beside_the_same_pipeline(
    tmp_path: Path, creator: Creator
) -> None:
    """ADR 0022 amendment: the sampling rate is part of a reference's identity.

    A 2 fps re-run of an original already imported at 1 fps on the *same* pipeline used to be
    skipped as already imported; it is a denser grid, so it imports beside the 1 fps row, inherits
    its cast, and `--compare` measures the moments both grids sample.
    """
    dense = synthetic(creators=("NAME",))
    write(tmp_path / "sparse.json", at_one_fps(synthetic()))
    run(str(tmp_path / "sparse.json"))
    sparse = Reference.objects.get()
    assert (sparse.pipeline, sparse.frame_fps) == (PIPELINE, 1.0)

    write(tmp_path / "dense.json", dense)
    comparison = run("--compare", str(tmp_path / "dense.json"))
    assert f"compare with reference {sparse.pk}: 16 pairs in both, 8 only in file" in comparison
    assert "distance 0 for 100.0%" in comparison and "fps differs: stored 1, file 2" in comparison
    assert Reference.objects.count() == 1

    output = run(str(tmp_path / "dense.json"))
    denser = Reference.objects.exclude(pk=sparse.pk).get()
    assert (denser.pipeline, denser.frame_fps, denser.sha256) == (PIPELINE, 2.0, sparse.sha256)
    assert f"alongside reference {sparse.pk} on {PIPELINE} at 1 fps" in output
    assert f"cast from reference {sparse.pk}" in output and denser.creator == creator
    # The same rate on the same pipeline is still only stored once.
    assert f"skipped (already imported as reference {denser.pk})" in run(
        str(tmp_path / "dense.json")
    )
    with pytest.raises(ValueError, match="at 2 fps"):
        import_fingerprint(creator=creator, fingerprint=dense)
    # The database says the same, photo collections (no fps) included.
    with pytest.raises(IntegrityError), transaction.atomic():
        Reference.objects.create(
            creator=creator, title="dup", sha256=sparse.sha256, frame_fps=1.0, pipeline=PIPELINE
        )
    photos: dict[str, Any] = {
        "creator": creator,
        "kind": "photos",
        "sha256": "cd" * 32,
        "pipeline": PHOTO_PIPELINE,
    }
    Reference.objects.create(title="set", **photos)
    with pytest.raises(IntegrityError), transaction.atomic():
        Reference.objects.create(title="set again", **photos)


def test_a_given_digest_is_checked_by_stat_without_a_second_read(
    original: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = original.stat()
    digest = file_sha256(original)

    def no_read(path: Path) -> str:
        raise AssertionError("the file was read again")

    monkeypatch.setattr(fingerprint_module, "file_sha256", no_read)
    result = fingerprint_video(original, sha256=digest, file_stat=before, dihedral=False)
    assert (result.sha256, result.file_size) == (digest, before.st_size)

    # A write after the digest was taken moves the modification time: refused, still no read.
    touched = before.st_mtime_ns + 1_000_000_000
    os.utime(original, ns=(touched, touched))
    with pytest.raises(ValueError, match="changed"):
        fingerprint_video(original, sha256=digest, file_stat=before, dihedral=False)


def test_client_writes_a_denser_run_beside_the_default_one(
    tmp_path: Path, original: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "fingerprints"
    client = load_client()
    base = ["--jobs", "1", "--out", str(out), "--no-dihedral", str(original)]

    assert client.main(base) == 0
    assert client.main(["--fps", "2", *base]) == 0
    assert "skipped clip" not in capsys.readouterr().err
    names = sorted(path.name for path in out.iterdir())
    digest = file_sha256(original)[:12]
    assert names == [f"clip.{digest}.2fps.json", f"clip.{digest}.json"]
    dense = from_json(json.loads((out / names[0]).read_text()))
    assert (dense.fps, dense.frame_count) == (2.0, 6)
    assert client.main(["--fps", "2", *base]) == 0
    assert "skipped clip.mp4" in capsys.readouterr().err
