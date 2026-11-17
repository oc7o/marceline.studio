"""Signed envelopes: only development's key, only the expected format and schema version."""

import base64
import json
from pathlib import Path

import pytest

from delivery.envelope import (
    FORMAT_BUNDLE,
    FORMAT_PURGE,
    SIGNING_DOMAIN,
    EnvelopeError,
    generate_private_key,
    load_private_key,
    load_public_key,
    open_envelope,
    seal,
)
from delivery.tests.helpers import bundle_payload, bundle_wire, make_keys


def test_a_sealed_payload_opens_with_the_matching_public_key(tmp_path: Path) -> None:
    keys = make_keys(tmp_path)

    payload = open_envelope(
        bundle_wire(keys.private, 3),
        load_public_key(keys.public_path),
        expected_format=FORMAT_BUNDLE,
    )

    assert payload["organisation_id"] == 3
    assert load_private_key(keys.private_path).public_key() == keys.private.public_key()


def _tamper(wire: bytes, change: dict[str, object]) -> bytes:
    outer = json.loads(wire)
    payload = json.loads(base64.b64decode(outer["payload"]))
    payload.update(change)
    outer["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
    return json.dumps(outer).encode()


def test_a_tampered_payload_is_refused(tmp_path: Path) -> None:
    keys = make_keys(tmp_path)
    wire = _tamper(bundle_wire(keys.private, 3), {"organisation_id": 4})

    with pytest.raises(EnvelopeError) as refused:
        open_envelope(wire, keys.private.public_key(), expected_format=FORMAT_BUNDLE)

    assert refused.value.code == "bad_signature"


def test_a_payload_signed_by_another_key_is_refused(tmp_path: Path) -> None:
    keys = make_keys(tmp_path)
    impostor = generate_private_key()

    with pytest.raises(EnvelopeError) as refused:
        open_envelope(
            bundle_wire(impostor, 3), keys.private.public_key(), expected_format=FORMAT_BUNDLE
        )

    assert refused.value.code == "bad_signature"


def test_another_format_is_refused_even_when_signed(tmp_path: Path) -> None:
    keys = make_keys(tmp_path)

    with pytest.raises(EnvelopeError) as refused:
        open_envelope(
            bundle_wire(keys.private, 3), keys.private.public_key(), expected_format=FORMAT_PURGE
        )

    assert refused.value.code == "wrong_format"


def test_another_schema_version_is_refused_even_when_signed(tmp_path: Path) -> None:
    keys = make_keys(tmp_path)
    body = json.dumps({**bundle_payload(3), "schema_version": 2}, default=str).encode()
    wire = json.dumps(
        {
            "alg": "Ed25519",
            "payload": base64.b64encode(body).decode(),
            "signature": base64.b64encode(keys.private.sign(SIGNING_DOMAIN + body)).decode(),
        }
    ).encode()

    with pytest.raises(EnvelopeError) as refused:
        open_envelope(wire, keys.private.public_key(), expected_format=FORMAT_BUNDLE)

    assert refused.value.code == "wrong_schema_version"


def test_seal_refuses_an_unversioned_payload(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        seal({"format": FORMAT_BUNDLE, "schema_version": 2}, generate_private_key())


def test_the_signature_is_domain_separated(tmp_path: Path) -> None:
    """A bare Ed25519 signature over the payload (no domain prefix) does not verify."""
    keys = make_keys(tmp_path)
    body = json.dumps(bundle_payload(3), default=str).encode()
    wire = json.dumps(
        {
            "alg": "Ed25519",
            "payload": base64.b64encode(body).decode(),
            "signature": base64.b64encode(keys.private.sign(body)).decode(),
        }
    ).encode()

    with pytest.raises(EnvelopeError):
        open_envelope(wire, keys.private.public_key(), expected_format=FORMAT_BUNDLE)


@pytest.mark.parametrize(
    "wire",
    [
        b"",
        b"not json",
        b"[]",
        b'{"alg": "HS256", "payload": "", "signature": ""}',
        b'{"alg": "Ed25519", "payload": "***", "signature": "***"}',
    ],
)
def test_malformed_envelopes_are_refused(tmp_path: Path, wire: bytes) -> None:
    with pytest.raises(EnvelopeError) as refused:
        open_envelope(wire, generate_private_key().public_key(), expected_format=FORMAT_BUNDLE)

    assert refused.value.code == "malformed"
