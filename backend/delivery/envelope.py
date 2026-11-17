"""Signed envelopes between development and production (ADR 0028, "What the signature protects").

Development signs every bundle, accounts document and purge with an Ed25519 key only it holds;
production holds the public key and refuses anything that does not verify. For customer writes the
direction reverses once: production signs the batches development pulls with its own key, and
development holds that public key (ADR 0028 amendment "The production write path").

Tailscale already authenticates the path; the signature covers what it does not — a spoofed or
compromised tailnet node, a keying bug that files organisation A's bundle under B, replayed old
data, forged purges.

Wire format (JSON): `{"alg": "Ed25519", "payload": <base64>, "signature": <base64>}`. The signature
covers a fixed domain prefix plus the payload's exact bytes, so nothing has to be canonicalised and
a payload can never be re-read as some other protocol's message. Inside the payload, `format` and
`schema_version` say what it is; a receiver names the one format it expects and refuses the rest.

Pure: no Django settings, no database, no clock. Callers pass keys and `now`.
"""

import base64
import binascii
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ALG = "Ed25519"
SIGNING_DOMAIN = b"marceline-envelope-v1\n"
SCHEMA_VERSION = 1

FORMAT_BUNDLE = "marceline.customer-bundle"
FORMAT_ACCOUNTS = "marceline.accounts"
FORMAT_PURGE = "marceline.cache-purge"
#: Customer writes (ADR 0028 amendment "The production write path"). Development signs its pull
#: requests and acknowledgements with its key; production signs the batches of customer writes it
#: hands out with a key of its own, so neither machine ever holds the other's private key.
FORMAT_OUTBOX_PULL = "marceline.outbox-pull"
FORMAT_OUTBOX_BATCH = "marceline.outbox-batch"
FORMAT_OUTBOX_ACK = "marceline.outbox-ack"

#: Largest envelope production accepts. A bundle is kilobytes to a few megabytes (ADR 0028).
MAX_ENVELOPE_BYTES = 32 * 1024 * 1024


class EnvelopeError(Exception):
    """Verification failed. `code` is safe to log; it never carries payload content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_pem(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise EnvelopeError("not_an_ed25519_private_key")
    return key


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise EnvelopeError("not_an_ed25519_public_key")
    return key


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def seal(payload: dict[str, Any], key: Ed25519PrivateKey) -> bytes:
    """Sign a payload; returns the wire bytes. `format` and `schema_version` must be set."""
    if "format" not in payload or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("payload needs a format and the current schema_version")
    body = json.dumps(payload, default=_json_default, separators=(",", ":")).encode()
    signature = key.sign(SIGNING_DOMAIN + body)
    return json.dumps(
        {
            "alg": ALG,
            "payload": base64.b64encode(body).decode(),
            "signature": base64.b64encode(signature).decode(),
        }
    ).encode()


def open_envelope(wire: bytes, key: Ed25519PublicKey, *, expected_format: str) -> dict[str, Any]:
    """Verify the signature and the format/schema version; returns the payload as parsed JSON.

    Field-level checks (organisation, times, nonces) belong to the receiver of each format.
    """
    if len(wire) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError("too_large")
    try:
        outer = json.loads(wire)
    except (ValueError, UnicodeDecodeError) as exc:
        raise EnvelopeError("malformed") from exc
    if not isinstance(outer, dict) or outer.get("alg") != ALG:
        raise EnvelopeError("malformed")
    try:
        body = base64.b64decode(str(outer.get("payload", "")), validate=True)
        signature = base64.b64decode(str(outer.get("signature", "")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EnvelopeError("malformed") from exc
    try:
        key.verify(signature, SIGNING_DOMAIN + body)
    except InvalidSignature as exc:
        raise EnvelopeError("bad_signature") from exc
    try:
        payload = json.loads(body)
    except ValueError as exc:  # pragma: no cover - we signed it, so it parsed when we did
        raise EnvelopeError("malformed") from exc
    if not isinstance(payload, dict) or payload.get("format") != expected_format:
        raise EnvelopeError("wrong_format")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise EnvelopeError("wrong_schema_version")
    return payload
