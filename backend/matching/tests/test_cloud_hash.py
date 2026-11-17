"""The contained decoder for customer uploads (issue #59, ADR 0044).

Three kinds of claim, and each one is silent if it breaks:

1. **The commands.** `-protocol_whitelist file` is what stops a crafted playlist turning ffmpeg
   into a fetcher or a file reader. A reviewer cannot see that it is missing; a test can. Both
   argument lists are therefore asserted element by element.
2. **The containment.** The child gets no database credential and may not create a file. Those are
   properties of the environment and the rlimits, so they are asserted directly — and, for
   `RLIMIT_FSIZE`, exercised: a real child that tries to write is refused.
3. **The hashes.** A cloud-fingerprinted original has to be bit-identical with one the CLI or
   `add_reference` would produce from the same file, or the library stops being comparable. The
   clip is generated with ffmpeg at test time (no media in the repository, ever).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from matching import cloud_hash
from matching.cloud_hash import CloudHashError, Probe
from matching.fingerprint import PIPELINE, fingerprint_video, to_json
from matching.tests.test_frames import make_video

#: Two seconds at 10 fps, small enough to hash in a test and big enough to have three sampled
#: moments at 1 fps.
CLIP = "testsrc2=duration=3:size=320x240:rate=10"


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    make_video(tmp_path, CLIP)
    return tmp_path / "clip.mp4"


def a_probe(**overrides: object) -> Probe:
    fields: dict[str, object] = {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration_s": 60.0,
        "streams": 2,
        "codec": "h264",
        "width": 1920,
        "height": 1080,
    }
    fields.update(overrides)
    return Probe(**fields)  # type: ignore[arg-type]


# --- the commands ----------------------------------------------------------------------------


def test_the_probe_command_is_exactly_this() -> None:
    assert cloud_hash.probe_command(Path("/spool/abc/file")) == [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-show_entries",
        "format=format_name,duration,nb_streams:stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        "/spool/abc/file",
    ]


def test_the_probe_command_allows_only_the_file_protocol() -> None:
    """A `concat:`/`http:` input inside a crafted file is how ffprobe is made to fetch or read."""
    command = cloud_hash.probe_command(Path("/spool/abc/file"))
    assert command[command.index("-protocol_whitelist") + 1] == "file"


def test_the_decoder_command_is_exactly_this_and_carries_no_customer_input() -> None:
    assert cloud_hash.decoder_command("/opt/venv/bin/python") == [
        "/opt/venv/bin/python",
        "-E",
        "-s",
        "-m",
        "matching.cloud_decoder",
    ]


def test_the_decoding_command_built_by_frames_still_whitelists_only_files() -> None:
    """`fingerprint_video` decodes through `matching.frames.iter_video_frames`, so the second
    ffmpeg call is that module's. If someone loosens it there, this feature is the one that pays."""
    source = (Path(__file__).resolve().parents[1] / "frames.py").read_text()
    assert '"-protocol_whitelist",\n        "file",' in source
    assert '"-nostdin",' in source


# --- the containment -------------------------------------------------------------------------


def test_the_child_gets_no_credential() -> None:
    parent = {
        "PATH": "/opt/venv/bin:/usr/bin",
        "POSTGRES_PASSWORD": "not-this",
        "POSTGRES_HOST": "db",
        "DJANGO_SECRET_KEY": "nor-this",
        "CACHE_SIGNING_KEY_PATH": "/run/marceline/signing.key",
        "OPENROUTER_API_KEY": "nor-this-either",
    }
    environment = cloud_hash.child_environment(parent)
    assert set(environment) == set(cloud_hash.CHILD_ENVIRONMENT_KEYS)
    assert environment["PATH"] == "/opt/venv/bin:/usr/bin"
    assert "not-this" not in "".join(environment.values())
    for name in environment:
        assert "POSTGRES" not in name and "SECRET" not in name and "KEY" not in name


def test_the_child_environment_falls_back_rather_than_inheriting() -> None:
    assert cloud_hash.child_environment({})["PATH"] == cloud_hash.FALLBACK_PATH


def test_the_decoder_may_not_create_a_file_at_all() -> None:
    assert dict(cloud_hash.rlimits())[__import__("resource").RLIMIT_FSIZE] == (0, 0)


def test_a_child_under_those_limits_cannot_write(tmp_path: Path) -> None:
    """Exercised rather than asserted: `RLIMIT_FSIZE = 0` is what makes "no frame ever leaves the
    worker" a property of the process instead of a promise about the code."""
    target = tmp_path / "frame.ppm"
    result = subprocess.run(
        [sys.executable, "-c", f"open({str(target)!r}, 'wb').write(b'x')"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=cloud_hash.child_environment(),
        preexec_fn=cloud_hash.apply_rlimits,
    )
    assert "File too large" in result.stderr
    assert not target.exists() or target.stat().st_size == 0


def test_the_limits_are_the_four_that_matter() -> None:
    resource = __import__("resource")
    assert {which for which, _ in cloud_hash.rlimits()} == {
        resource.RLIMIT_AS,
        resource.RLIMIT_CPU,
        resource.RLIMIT_FSIZE,
        resource.RLIMIT_NOFILE,
    }


def test_the_decoder_module_imports_no_django() -> None:
    """It runs without database credentials; an import of Django would be a reason to give it
    some, and a reason for an exploit to look for them."""
    source = (Path(__file__).resolve().parents[1] / "cloud_decoder.py").read_text()
    imports = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "django" in line.lower()
    ]
    assert imports == []


# --- the allowlist ---------------------------------------------------------------------------


def test_a_plain_master_is_admitted() -> None:
    assert cloud_hash.refusal(a_probe()) is None


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"format_name": "gif"}, "unsupported_container"),
        ({"format_name": "concat"}, "unsupported_container"),
        ({"codec": "gif"}, "unsupported_codec"),
        ({"streams": 400}, "too_many_streams"),
        ({"width": 16000, "height": 9000}, "frame_too_large"),
        ({"width": 0}, "frame_too_large"),
        ({"duration_s": 5 * 60 * 60}, "too_long"),
        ({"duration_s": 0.0}, "too_long"),
    ],
)
def test_what_is_refused_before_a_decoder_starts(overrides: dict[str, object], code: str) -> None:
    assert cloud_hash.refusal(a_probe(**overrides)) == code


def test_the_formats_a_browser_cannot_open_are_the_ones_on_the_list() -> None:
    """ProRes and DNxHD are the reason the feature exists; dropping them would empty it."""
    assert {"prores", "dnxhd"} <= cloud_hash.ALLOWED_CODECS


def test_a_file_without_a_video_stream_is_refused_by_the_parser() -> None:
    payload = json.dumps(
        {
            "format": {"format_name": "matroska,webm", "duration": "5", "nb_streams": 1},
            "streams": [{"codec_type": "audio", "codec_name": "opus"}],
        }
    )
    with pytest.raises(CloudHashError) as raised:
        cloud_hash.parse_probe(payload)
    assert raised.value.code == "no_video_stream"


def test_probe_output_is_parsed_into_the_five_facts() -> None:
    payload = json.dumps(
        {
            "format": {"format_name": "matroska,webm", "duration": "12.5", "nb_streams": 3},
            "streams": [
                {"codec_type": "audio", "codec_name": "opus"},
                {"codec_type": "video", "codec_name": "vp9", "width": 1280, "height": 720},
            ],
        }
    )
    probe = cloud_hash.parse_probe(payload)
    assert (probe.format_name, probe.codec, probe.duration_s) == ("matroska,webm", "vp9", 12.5)
    assert (probe.width, probe.height, probe.streams, probe.pixels) == (1280, 720, 3, 921_600)


def test_a_probe_answer_that_is_not_json_is_a_refusal_not_a_crash() -> None:
    with pytest.raises(CloudHashError) as raised:
        cloud_hash.parse_probe("ffprobe: no such file")
    assert raised.value.code == "unreadable_file"


# --- the hashes ------------------------------------------------------------------------------


def test_the_cloud_path_produces_the_same_document_as_the_local_one(clip: Path) -> None:
    """The whole point: an original hashed on the server is bit-identical with one the CLI or
    `add_reference` produced from the same file, on the same pipeline tag."""
    import hashlib

    digest = hashlib.sha256(clip.read_bytes()).hexdigest()
    here = to_json(
        fingerprint_video(clip, fps=1.0, title="a title", creators=["A Creator"], sha256=digest)
    )
    there = cloud_hash.hash_file(
        clip, fps=1.0, title="a title", creators=["A Creator"], sha256=digest
    )
    assert there["pipeline"] == PIPELINE
    # `client` records the machine that hashed it and is metadata, not hashes.
    here.pop("client"), there.pop("client")
    assert there == here


def test_a_refused_file_never_reaches_the_hasher(tmp_path: Path) -> None:
    not_a_video = tmp_path / "file"
    not_a_video.write_bytes(b"not a video at all")
    with pytest.raises(CloudHashError) as raised:
        cloud_hash.hash_file(not_a_video, fps=1.0, title="t", creators=["A"], sha256="0" * 64)
    assert raised.value.code in {"unreadable_file", "unsupported_container", "no_video_stream"}


def test_the_digest_is_the_servers_own_and_is_trusted_by_the_decoder(clip: Path) -> None:
    """The decoder does not re-read 10 GB to check a digest, and does not need to: the digest was
    computed by the spool while the chunks arrived, and the customer's claimed one was compared
    with it before the job was queued. What the decoder still refuses is a file that *changes*
    under it (size or modification time), which `fingerprint_video` checks either way."""
    document = cloud_hash.hash_file(clip, fps=1.0, title="t", creators=["A"], sha256="0" * 64)
    assert document["sha256"] == "0" * 64


def test_the_spec_carries_the_path_and_nothing_a_customer_typed_into_an_argument() -> None:
    spec = cloud_hash.decoder_spec(
        Path("/spool/abc/file"), fps=1.0, title="; rm -rf /", creators=["--help"], sha256="0" * 64
    )
    assert spec["path"] == "/spool/abc/file"
    # The title and the cast travel inside JSON on stdin, never as an argument to anything.
    assert "; rm -rf /" not in " ".join(cloud_hash.decoder_command())


def test_the_decoder_runs_from_the_package_root_so_the_module_is_importable() -> None:
    root = Path(cloud_hash.__file__).resolve().parents[1]
    assert (root / "matching" / "cloud_decoder.py").exists()
    assert os.path.isdir(root)


# --- progress across the containment boundary (ADR 0044 amendment 2026-09-18) -----------------


def test_a_progress_line_is_two_numbers_and_survives_the_round_trip() -> None:
    line = cloud_hash.progress_line(7, 300)
    assert json.loads(line) == {"progress": {"frames": 7, "total": 300}}
    assert cloud_hash.parse_progress(json.loads(line)) == (7, 300)
    assert cloud_hash.parse_progress(json.loads(cloud_hash.progress_line(3, None))) == (3, None)


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "fingerprint": {}},
        {"progress": "almost done"},
        {"progress": {"frames": "many"}},
        {"progress": {}},
        {"progress": {"frames": -1}},
        ["progress", 1],
        "progress",
    ],
)
def test_anything_that_is_not_two_numbers_is_not_progress(payload: object) -> None:
    """Besides the document, a pair of integers is the only thing that crosses back from the
    process holding the customer's file. A line that cannot be reduced to two integers is dropped
    — it never becomes a row, a log line or a word on a screen."""
    assert cloud_hash.parse_progress(payload) is None


def test_a_nonsense_total_reads_as_no_total_rather_than_a_wrong_denominator() -> None:
    assert cloud_hash.parse_progress({"progress": {"frames": 5, "total": 0}}) == (5, None)
    assert cloud_hash.parse_progress({"progress": {"frames": 5, "total": "3600"}}) == (5, 3600)
    assert cloud_hash.parse_progress({"progress": {"frames": 5, "total": "soon"}}) == (5, None)


def test_the_total_is_estimated_from_the_duration_or_is_not_claimed_at_all() -> None:
    assert cloud_hash.expected_positions(60.0, 1.0) == 60
    assert cloud_hash.expected_positions(60.0, 2.0) == 120
    assert cloud_hash.expected_positions(0.4, 1.0) == 1
    for impossible in ((0.0, 1.0), (-3.0, 1.0), (60.0, 0.0)):
        assert cloud_hash.expected_positions(*impossible) is None


def test_the_decoder_reports_its_progress_while_it_hashes(clip: Path) -> None:
    """End to end over the real pipe: the child hashes a generated clip and the parent is told how
    far it has come, in numbers, before the document arrives."""
    seen: list[tuple[int, int | None]] = []
    document = cloud_hash.hash_file(
        clip,
        fps=1.0,
        title="t",
        creators=["A"],
        sha256="0" * 64,
        on_progress=lambda frames, total: seen.append((frames, total)),
    )
    assert seen, "the decoder reported nothing at all"
    # The first line comes from the probe: nothing is claimed before there is a duration.
    assert seen[0] == (0, cloud_hash.expected_positions(3.0, 1.0))
    assert [frames for frames, _ in seen] == sorted(frames for frames, _ in seen)
    assert all(isinstance(frames, int) and total in (None, 3) for frames, total in seen)
    # The counter ends at what was really hashed, whatever the estimate said.
    assert max(frames for frames, _ in seen) >= len(document["frames"]) // 8 >= 1
