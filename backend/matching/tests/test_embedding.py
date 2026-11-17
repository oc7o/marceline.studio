"""The sketch itself: 64 bytes of sign bits, the model pin, and the memory budget (ADR 0037)."""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from index.models import EMBEDDING_SKETCH_BYTES
from matching import embedding
from matching.tests.images import synthetic_image


def test_the_sketch_is_64_bytes_of_sign_bits() -> None:
    vector = np.array([[1.0, -1.0] * 256], dtype=np.float32)
    (sketch,) = embedding._sign_sketches(vector)
    assert len(sketch) == embedding.SKETCH_BYTES == 64
    # Most significant bit first, like a PDQ hash: 1, 0, 1, 0, ... is 0b10101010 per byte.
    assert set(sketch) == {0b10101010}


def test_a_zero_vector_has_no_bits_set_and_does_not_divide_by_zero() -> None:
    (sketch,) = embedding._sign_sketches(np.zeros((1, 512), dtype=np.float32))
    assert sketch == bytes(64)


def test_normalising_cannot_change_the_sign_bits() -> None:
    """The stored form is the sign of a normalised vector, and scaling is sign-preserving."""
    rng = np.random.default_rng(7)
    vector = rng.normal(size=(4, 512)).astype(np.float32)
    assert embedding._sign_sketches(vector) == embedding._sign_sketches(vector * 17.5)


def test_a_wrongly_shaped_encoder_output_is_refused() -> None:
    with pytest.raises(embedding.EmbeddingModelInvalid):
        embedding._sign_sketches(np.zeros((1, 384), dtype=np.float32))


def test_the_index_and_the_signal_agree_on_the_sketch_size() -> None:
    """`index` may not import `matching`, so the two constants are kept equal by this test."""
    assert EMBEDDING_SKETCH_BYTES == embedding.SKETCH_BYTES


def test_sketch_distance_counts_bits_and_refuses_the_wrong_length() -> None:
    first = bytes([0b11110000]) + bytes(63)
    second = bytes([0b00000000]) + bytes(63)
    assert embedding.sketch_distance(first, second) == 4
    assert embedding.sketch_distance(first, first) == 0
    with pytest.raises(ValueError):
        embedding.sketch_distance(first, bytes(32))


def test_sketches_to_words_views_the_same_bytes() -> None:
    sketches = [bytes(range(64)), bytes(range(64, 128))]
    words = embedding.sketches_to_words(sketches)
    assert words.shape == (2, 8)
    assert words.view(np.uint8).tobytes() == b"".join(sketches)
    assert embedding.sketches_to_words([]).shape == (0, 8)
    with pytest.raises(ValueError):
        embedding.sketches_to_words([bytes(32)])


def test_preprocess_centre_crops_to_the_encoder_input() -> None:
    tensor = embedding.preprocess(synthetic_image(1, size=(800, 450)))
    assert tensor.shape == (1, 3, embedding.INPUT_SIZE, embedding.INPUT_SIZE)
    assert tensor.dtype == np.float32
    assert float(tensor.min()) >= 0.0 and float(tensor.max()) <= 1.0


def test_preprocess_handles_a_picture_smaller_than_the_input() -> None:
    tensor = embedding.preprocess(Image.new("RGB", (40, 30), (10, 20, 30)))
    assert tensor.shape == (1, 3, embedding.INPUT_SIZE, embedding.INPUT_SIZE)


def test_a_missing_model_fails_loudly_and_says_what_to_do(tmp_path: Path) -> None:
    missing = tmp_path / "nope.onnx"
    with pytest.raises(embedding.EmbeddingModelMissing) as caught:
        embedding.verify_model(missing)
    message = str(caught.value)
    assert "fetch_embedding_model" in message
    assert embedding.MODEL_SHA256 in message
    assert "never silently skipped" in message


def test_a_model_of_the_wrong_size_or_digest_is_refused(tmp_path: Path) -> None:
    wrong_size = tmp_path / "small.onnx"
    wrong_size.write_bytes(b"not a model")
    with pytest.raises(embedding.EmbeddingModelInvalid, match="bytes, expected"):
        embedding.verify_model(wrong_size)
    wrong_digest = tmp_path / "right-size.onnx"
    wrong_digest.write_bytes(bytes(embedding.MODEL_BYTES))
    with pytest.raises(embedding.EmbeddingModelInvalid, match="sha256"):
        embedding.verify_model(wrong_digest)


def test_model_path_prefers_the_configured_location(tmp_path: Path) -> None:
    assert embedding.model_path(tmp_path / "m.onnx") == tmp_path / "m.onnx"
    assert embedding.model_path("") == embedding.DEFAULT_MODEL_DIR / embedding.MODEL_FILENAME
    assert embedding.model_path(None).name == embedding.MODEL_FILENAME


# --- the graph check: this is what "image encoder only" means in code ---

GOOD_INPUT = [("pixel_values", ["batch_size", 3, 256, 256])]
GOOD_OUTPUT = [("image_embeds", ["batch_size", 512])]


def test_the_pinned_graph_passes() -> None:
    embedding.check_graph(GOOD_INPUT, GOOD_OUTPUT)


def test_a_second_input_is_refused_as_a_text_tower() -> None:
    with pytest.raises(embedding.EmbeddingModelInvalid, match="text tower"):
        embedding.check_graph(
            [*GOOD_INPUT, ("input_ids", ["batch_size", 77])],
            GOOD_OUTPUT,
        )


def test_a_second_output_is_refused_as_a_head() -> None:
    with pytest.raises(embedding.EmbeddingModelInvalid, match="one output"):
        embedding.check_graph(GOOD_INPUT, [*GOOD_OUTPUT, ("logits", ["batch_size", 1000])])


def test_an_output_of_another_width_is_refused() -> None:
    with pytest.raises(embedding.EmbeddingModelInvalid, match=r"\[batch, 512\]"):
        embedding.check_graph(GOOD_INPUT, [("boxes", ["batch_size", 4])])


def test_an_input_of_another_shape_is_refused() -> None:
    with pytest.raises(embedding.EmbeddingModelInvalid, match="expected \\[batch, 3, 256, 256\\]"):
        embedding.check_graph([("pixel_values", ["batch_size", 3, 224, 224])], GOOD_OUTPUT)


# --- memory: the number of hashing processes is a memory decision (ADR 0037) ---


def test_processes_that_fit_leaves_headroom() -> None:
    # 2 GB available: (2048 - 256) // 160 = 11 models fit, so two processes certainly do.
    assert embedding.processes_that_fit(2, 2048.0) == 2
    # 500 MB: (500 - 256) // 160 = 1.
    assert embedding.processes_that_fit(2, 500.0) == 1
    assert embedding.processes_that_fit(2, 300.0) == 0
    assert embedding.processes_that_fit(0, 8000.0) == 0
    # Unknown memory: trust the operator rather than guess.
    assert embedding.processes_that_fit(4, 0.0) == 4


def test_hash_processes_reduces_and_says_so() -> None:
    count, note = embedding.hash_processes(2, embeddings=True, memory_mb=500.0)
    assert count == 1
    assert "starting 1" in note
    assert "500 MB available" in note


def test_hash_processes_is_silent_when_everything_fits() -> None:
    assert embedding.hash_processes(2, embeddings=True, memory_mb=4096.0) == (2, "")


def test_hash_processes_refuses_rather_than_let_the_lane_be_oom_killed() -> None:
    with pytest.raises(embedding.EmbeddingUnavailable, match="only 200 MB is available"):
        embedding.hash_processes(2, embeddings=True, memory_mb=200.0)


def test_hash_processes_ignores_memory_without_embeddings() -> None:
    assert embedding.hash_processes(2, embeddings=False, memory_mb=1.0) == (2, "")


def test_available_mb_reads_a_number_or_zero() -> None:
    assert embedding.available_mb() >= 0.0
