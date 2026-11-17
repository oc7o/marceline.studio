"""The frame embedding: one 512-bit sign sketch per picture, beside the PDQ hash (ADR 0037).

A second, *independent* content signal. PDQ dies on the transform that real sites actually apply
— an asymmetric 1-3 % edge trim costs it 44 bits (`docs/matching/second-signal.md` §4.6) — and a
small CPU vision encoder barely notices it. What comes out is deliberately tiny: the image
encoder of MobileCLIP-S0 through `onnxruntime`, the resulting 512-dimensional vector normalised
and immediately reduced to its 512 **sign bits** (64 bytes), which is all that is ever stored,
compared or passed on.

**An embedding is never evidence.** Measured over 805,683 real negative pairs, the nearest
unrelated pair scores *above* the median of a genuine same-picture pair, so there is no safe fixed
threshold and no operating point. Its only job is ranking: it proposes candidates that PDQ, the
timeline, a duration, a cluster or a human then confirms (`matching.embedding_candidates`).

**What this is not, structurally rather than by promise.** No face detection, no face recognition,
no biometric or body measurement, no person identification, no age estimation — not as an output
and not as an intermediate step:

- the model file is pinned by digest and is the **image encoder only**; MobileCLIP's text tower is
  not shipped, so the model cannot be asked a question about a picture at all;
- `verify_model` refuses any file whose graph is not exactly one image input and one
  512-dimensional embedding output — no classifier head, no detector, no landmarks, no text;
- the session's output has exactly **one** consumer, `_sign_sketches`. Nothing public here returns
  a float vector, so no caller can build a classifier on top of one;
- the only operation ever performed on a stored sketch is a Hamming distance to another sketch.

`matching/tests/test_embedding_boundaries.py` pins all four.

No Django and no `onnxruntime` at import time: `client/fingerprint.py` imports this module on the
operator's Mac, and the media worker imports it in a hash child. The runtime import happens when
an encoder is first built, and a missing model or wheel fails **loudly** with an actionable
message rather than silently skipping the signal.
"""

import hashlib
import os
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PIL import Image

# Recipe tag, in the spirit of ADR 0022: model, precision, reduction and version. A page sketch is
# only ever compared with a reference sketch carrying the same tag. Changing the model, the
# preprocessing or the reduction is a new tag and a re-embedding, never a reinterpretation.
EMBEDDING_PIPELINE = "mobileclip-s0-fp32-sign512-v1"
EMBEDDING_PIPELINES = frozenset({EMBEDDING_PIPELINE})

SKETCH_BITS = 512
SKETCH_BYTES = SKETCH_BITS // 8
EMBEDDING_DIMS = 512
INPUT_SIZE = 256

# The model file. Kept out of the repository and out of the container image on purpose (45 MB of
# weights are not source code), fetched once per machine by `manage.py fetch_embedding_model` and
# pinned by digest, so "the model" is one exact artefact everywhere.
MODEL_FILENAME = "mobileclip_s0_fp32.onnx"
MODEL_URL = "https://huggingface.co/Xenova/mobileclip_s0/resolve/main/onnx/vision_model.onnx"
MODEL_SHA256 = "17d3c037b1d488c10c50e09f6009ea5a198caef4e0e8f4ea5617b7cb2d067ac0"
MODEL_BYTES = 45_543_630
# What one loaded session adds to a process's resident set. Memory, not CPU, is the scarce
# resource here (ADR 0037). Measured with this module on the check runner, 2026-09-17: +117 MB on
# building the session and +121 MB peak while hashing, identical at 1, 2 and 4 threads (a whole
# hash child then sits at ~160 MB). `docs/matching/second-signal.md` §5.1 reported 288 MB for a
# whole harness process; the budget below is the measured session cost plus margin for arena
# growth over a child's 500 jobs.
MODEL_RSS_MB = 160.0
# Leave this much of MemAvailable untouched when deciding how many hashing processes fit.
RSS_HEADROOM_MB = 256.0

# Where the model lives unless a setting says otherwise. Outside the repository, like the rights
# vault and the signing keys.
DEFAULT_MODEL_DIR = Path.home() / ".cache" / "marceline" / "models"

# onnxruntime threads per session. One: the media lane already runs several hashing processes and
# FAISS/Postgres/crawlers share four cores on the development box.
DEFAULT_THREADS = 1


class EmbeddingModelMissing(RuntimeError):
    """The ONNX model file is not where it should be. Never skipped silently."""


class EmbeddingModelInvalid(RuntimeError):
    """The file at the model path is not the pinned image encoder."""


class EmbeddingUnavailable(RuntimeError):
    """`onnxruntime` is not installed. Never skipped silently."""


def model_path(configured: str | os.PathLike[str] | None = None) -> Path:
    """The model file's path: the configured one, or the default cache location."""
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_MODEL_DIR / MODEL_FILENAME


def file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def missing_model_message(path: Path) -> str:
    return (
        f"the frame-embedding model is missing: {path} does not exist.\n"
        f"Fetch it once with `manage.py fetch_embedding_model` (downloads {MODEL_BYTES} bytes "
        f"from {MODEL_URL} and checks sha256 {MODEL_SHA256}), or point "
        f"MATCHING_EMBEDDING_MODEL_PATH at an existing copy. Embeddings are never computed from "
        f"another file and never silently skipped (ADR 0037)."
    )


def verify_model(path: Path) -> None:
    """Refuse anything but the pinned image encoder, before a session is ever built.

    Three independent checks, in the order that fails cheapest: size, digest, and then the graph's
    own shape — exactly one image input and exactly one `(batch, 512)` output. The graph check is
    what pins "image encoder only": a text tower would add an input, a classifier or detector head
    a differently shaped output.
    """
    if not path.is_file():
        raise EmbeddingModelMissing(missing_model_message(path))
    size = path.stat().st_size
    if size != MODEL_BYTES:
        raise EmbeddingModelInvalid(
            f"{path} is {size} bytes, expected {MODEL_BYTES} (ADR 0037 pins one exact model)"
        )
    digest = file_digest(path)
    if digest != MODEL_SHA256:
        raise EmbeddingModelInvalid(
            f"{path} has sha256 {digest}, expected {MODEL_SHA256}; refusing to embed with an "
            "unpinned model"
        )


def check_graph(inputs: Sequence[tuple[str, Sequence[object]]], outputs: Sequence[Any]) -> None:
    """Pure part of the graph check, so a test can exercise it without a model file.

    `inputs` are (name, shape) pairs and `outputs` (name, shape) pairs, as onnxruntime reports
    them (a symbolic batch dimension is a string).
    """
    if len(inputs) != 1:
        raise EmbeddingModelInvalid(
            f"the model must take exactly one input (the picture), got {[n for n, _ in inputs]}; "
            "a second input would mean a text tower, which this decision does not ship"
        )
    shape = list(inputs[0][1])
    if len(shape) != 4 or list(shape[1:]) != [3, INPUT_SIZE, INPUT_SIZE]:
        raise EmbeddingModelInvalid(
            f"the model's input is {shape}, expected [batch, 3, {INPUT_SIZE}, {INPUT_SIZE}]"
        )
    if len(outputs) != 1:
        raise EmbeddingModelInvalid(
            f"the model must have exactly one output (the embedding), got "
            f"{[n for n, _ in outputs]}; extra outputs would be a head this decision does not use"
        )
    out_shape = list(outputs[0][1])
    if len(out_shape) != 2 or out_shape[1] != EMBEDDING_DIMS:
        raise EmbeddingModelInvalid(
            f"the model's output is {out_shape}, expected [batch, {EMBEDDING_DIMS}]"
        )


def preprocess(image: Image.Image) -> NDArray[np.float32]:
    """One picture into the encoder's input tensor: shortest side to 256, centre crop, 0..1.

    MobileCLIP-S0's ONNX export normalises inside the graph, so there is no mean/std here. The
    recipe is part of `EMBEDDING_PIPELINE`: changing it is a new tag.
    """
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    scale = INPUT_SIZE / min(rgb.size)
    width = max(INPUT_SIZE, round(rgb.width * scale))
    height = max(INPUT_SIZE, round(rgb.height * scale))
    resized = rgb.resize((width, height), Image.Resampling.BICUBIC)
    left = (width - INPUT_SIZE) // 2
    top = (height - INPUT_SIZE) // 2
    cropped = resized.crop((left, top, left + INPUT_SIZE, top + INPUT_SIZE))
    pixels = np.asarray(cropped, dtype=np.float32) / 255.0
    return np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])


def _sign_sketches(vectors: NDArray[np.float32]) -> list[bytes]:
    """The **only** consumer of the encoder's output: normalise, then keep the sign bits.

    64 bytes per picture, most significant bit first (`np.packbits`), so the bytes are directly
    comparable by Hamming distance in numpy and in a FAISS binary index — the same bit order PDQ
    hashes use. Measured: the sketch keeps recall@10 exactly (0.994, identical to fp32) while
    losing rank-1 precision a candidate generator does not need.
    """
    if vectors.ndim != 2 or vectors.shape[1] != EMBEDDING_DIMS:
        raise EmbeddingModelInvalid(
            f"the encoder returned {vectors.shape}, expected (n, {EMBEDDING_DIMS})"
        )
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    normalised = vectors / np.where(norms == 0, 1.0, norms)
    bits = (normalised > 0).astype(np.uint8)
    packed = np.packbits(bits, axis=1)
    return [row.tobytes() for row in packed]


def sketch_distance(first: bytes, second: bytes) -> int:
    """Hamming distance between two sketches — the only operation performed on one."""
    for value in (first, second):
        if len(value) != SKETCH_BYTES:
            raise ValueError(f"a sketch is {SKETCH_BYTES} bytes, got {len(value)}")
    return (int.from_bytes(first) ^ int.from_bytes(second)).bit_count()


class Encoder:
    """One loaded session. Build it once per process: the weights cost ~288 MB of resident set."""

    def __init__(
        self, path: str | os.PathLike[str] | None = None, *, threads: int = DEFAULT_THREADS
    ) -> None:
        self.path = model_path(path)
        verify_model(self.path)
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - the wheel is a hard dependency
            raise EmbeddingUnavailable(
                "onnxruntime is not installed, so no frame embedding can be computed. "
                "Install the backend dependencies (`make sync`), or on the operator's Mac "
                "`uv run --with onnxruntime client/fingerprint.py …` (client/README.md)."
            ) from exc
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, threads)
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(self.path), options, providers=["CPUExecutionProvider"]
        )
        check_graph(
            [(i.name, i.shape) for i in self._session.get_inputs()],
            [(o.name, o.shape) for o in self._session.get_outputs()],
        )
        self._input = self._session.get_inputs()[0].name

    def sketches(self, images: Sequence[Image.Image]) -> list[bytes]:
        """One 64-byte sketch per picture. Nothing else leaves this object."""
        if not images:
            return []
        batch = np.concatenate([preprocess(image) for image in images])
        # The session's output is consumed here and nowhere else (module docstring).
        return _sign_sketches(np.asarray(self._session.run(None, {self._input: batch})[0]))

    def sketch(self, image: Image.Image) -> bytes:
        return self.sketches([image])[0]


_lock = threading.Lock()
_encoder: Encoder | None = None


def encoder(
    path: str | os.PathLike[str] | None = None, *, threads: int = DEFAULT_THREADS
) -> Encoder:
    """The process's single encoder, built on first use (288 MB of weights, loaded once)."""
    global _encoder
    with _lock:
        if _encoder is None or (path and Path(path).expanduser() != _encoder.path):
            _encoder = Encoder(path, threads=threads)
        return _encoder


def reset_encoder() -> None:
    """Drop the process's encoder (tests, and a child recycled by the hash pool)."""
    global _encoder
    with _lock:
        _encoder = None


def available_mb() -> float:
    """`MemAvailable` in MiB (Linux `/proc/meminfo`); 0.0 where it cannot be read."""
    try:
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            for line in meminfo:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


def processes_that_fit(
    requested: int,
    memory_mb: float,
    *,
    per_process_mb: float = MODEL_RSS_MB,
    headroom_mb: float = RSS_HEADROOM_MB,
) -> int:
    """How many hashing processes may hold a model at once — pure, so it is testable.

    Memory is the scarce resource (ADR 0037): the development box has 1-2 GB spare and each
    process that embeds costs `per_process_mb` on top of its own decoding. `headroom_mb` is left
    for everything else. Returns 0 when not even one fits, and the caller then refuses to start
    with embeddings on rather than quietly OOM-killing the media lane.
    """
    if requested < 1:
        return 0
    if memory_mb <= 0:  # unknown memory: trust the operator's setting rather than guess
        return requested
    fits = int((memory_mb - headroom_mb) // max(per_process_mb, 1.0))
    return max(0, min(requested, fits))


def hash_processes(
    requested: int, *, embeddings: bool, memory_mb: float | None = None
) -> tuple[int, str]:
    """(processes to start, a line for the log). Raises when embeddings cannot fit at all."""
    if not embeddings:
        return max(1, requested), ""
    memory = available_mb() if memory_mb is None else memory_mb
    fits = processes_that_fit(requested, memory)
    if fits < 1:
        raise EmbeddingUnavailable(
            f"frame embeddings need {MODEL_RSS_MB:.0f} MB of resident set per hashing process "
            f"plus {RSS_HEADROOM_MB:.0f} MB of headroom, and only {memory:.0f} MB is available. "
            "Free memory, or turn MATCHING_EMBEDDINGS_ENABLED off."
        )
    if fits < requested:
        return fits, (
            f"embeddings: {requested} hash processes would need "
            f"{requested * MODEL_RSS_MB:.0f} MB of models; starting {fits} "
            f"({memory:.0f} MB available)"
        )
    return fits, ""


def sketches_to_words(sketches: Iterable[bytes]) -> NDArray[np.uint64]:
    """Sketch bytes as an (N, 8) uint64 array, the shape `matching.hamming` searches."""
    values = [bytes(value) for value in sketches]
    for value in values:
        if len(value) != SKETCH_BYTES:
            raise ValueError(f"a sketch is {SKETCH_BYTES} bytes, got {len(value)}")
    if not values:
        return np.empty((0, SKETCH_BYTES // 8), dtype=np.uint64)
    return np.frombuffer(b"".join(values), dtype=np.uint64).reshape(-1, SKETCH_BYTES // 8)
