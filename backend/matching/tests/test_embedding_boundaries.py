"""The structural guarantees of ADR 0037, pinned rather than promised.

"No face detection, no biometrics, no person identification, no age estimation" is worth nothing
as a sentence in a document. These tests are what makes it true of the code:

1. only the **image** encoder is shipped — the model is a `vision_model` export, pinned by digest,
   and nothing anywhere names a text encoder, tokenizer or prompt;
2. the encoder's output has exactly **one** consumer, the sign reduction, so no caller can get a
   float vector to build a classifier on;
3. only `matching/embedding.py` may open an onnxruntime session at all;
4. the only thing done with a stored sketch is a Hamming distance.

They are AST checks over the real source tree, in the spirit of
`marceline/tests/test_request_accounting_coverage.py`.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
MODULE = BACKEND / "matching" / "embedding.py"
CLIENT = BACKEND.parent / "client" / "fingerprint.py"


#: This file names every word it forbids, so it must never scan itself.
SELF = Path(__file__).resolve()


def source_files() -> list[Path]:
    return [
        path
        for path in BACKEND.rglob("*.py")
        if not {".venv", "migrations"} & set(path.relative_to(BACKEND).parts) and path != SELF
    ]


def test_the_model_is_the_vision_export_and_nothing_names_a_text_tower() -> None:
    text = MODULE.read_text()
    assert "vision_model.onnx" in text, "the pinned URL must be the image encoder's export"
    for forbidden in ("text_model", "tokenizer", "get_text_features", "encode_text", "logit_scale"):
        assert forbidden not in text
    # And nowhere else in the backend either.
    for path in source_files():
        body = path.read_text()
        for forbidden in ("get_text_features", "encode_text", "CLIPTokenizer"):
            assert forbidden not in body, f"{path} names a text tower"


def test_nothing_in_the_backend_looks_for_faces_or_ages() -> None:
    """A crude but honest tripwire: the vocabulary of the things this decision refuses to do."""
    forbidden = (
        "face_detect",
        "facenet",
        "arcface",
        "face_recognition",
        "detect_faces",
        "age_estimat",
        "biometric_template",
        "landmark_detect",
    )
    offenders = [
        f"{path.relative_to(BACKEND)}: {word}"
        for path in source_files()
        for word in forbidden
        if word in path.read_text()
    ]
    assert offenders == []


def test_only_the_embedding_module_opens_an_onnx_session() -> None:
    offenders = []
    for path in [*source_files(), CLIENT]:
        if path == MODULE:
            continue
        body = path.read_text()
        if "InferenceSession" in body or "import onnxruntime" in body:
            offenders.append(str(path))
    assert offenders == [], (
        f"an onnxruntime session may only be built in {MODULE.name}: {offenders}"
    )


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_the_session_output_has_exactly_one_consumer() -> None:
    """`session.run(...)` appears once, and its value goes straight into `_sign_sketches`."""
    tree = ast.parse(MODULE.read_text())
    runs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    ]
    assert len(runs) == 1, "the encoder must be invoked in exactly one place"
    consumer = _function(tree, "sketches")
    calls = [
        node.func.id
        for node in ast.walk(consumer)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "_sign_sketches" in calls
    # ... and that is the only thing the run's value is passed to.
    source = ast.get_source_segment(MODULE.read_text(), consumer) or ""
    assert source.count(".run(") == 1
    assert "_sign_sketches(np.asarray(self._session.run" in source.replace("\n", "").replace(
        "            ", ""
    )


def test_no_public_function_hands_out_a_float_vector() -> None:
    """Everything public returns bytes, ints or paths. A float vector never leaves the module."""
    tree = ast.parse(MODULE.read_text())
    public = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
    assert public, "the module should have public functions"
    # `preprocess` returns the encoder's *input* tensor and `sketches_to_words` a view of stored
    # sketch bytes. Every other public function must hand back bytes, numbers or paths — never an
    # embedding, so there is nothing for a classifier to be built on.
    allowed_arrays = {"preprocess", "sketches_to_words"}
    for node in public:
        returns = ast.unparse(node.returns) if node.returns is not None else "None"
        if node.name in allowed_arrays:
            continue
        assert "NDArray" not in returns, f"{node.name} hands out an array"
    assert not hasattr_public_encode(tree)


def hasattr_public_encode(tree: ast.Module) -> bool:
    """True if the Encoder class exposes anything but sketches (i.e. a vector accessor)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Encoder":
            methods = {
                item.name
                for item in node.body
                if isinstance(item, ast.FunctionDef) and not item.name.startswith("_")
            }
            return methods != {"sketch", "sketches"}
    raise AssertionError("Encoder not found")


def test_a_stored_sketch_is_only_ever_compared_by_hamming_distance() -> None:
    """No cosine, no dot product, no similarity function anywhere near the sketches."""
    for name in (
        "matching/embedding.py",
        "matching/sketch_index.py",
        "matching/embedding_candidates.py",
        "marceline/embedding_pilot.py",
    ):
        body = (BACKEND / name).read_text()
        for forbidden in ("cosine", "np.dot", "inner(", "IndexFlatIP", "normalize("):
            assert forbidden not in body, f"{name} scores sketches with {forbidden}"
