"""Throwaway keys and envelopes for the production cache tests. No real keys, no network."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from delivery.envelope import (
    FORMAT_BUNDLE,
    FORMAT_PURGE,
    SCHEMA_VERSION,
    generate_private_key,
    private_key_pem,
    public_key_pem,
    seal,
)
from delivery.store import CUSTOMER_CACHE_MAX_STALE

NOW = datetime.fromisoformat("2026-09-17T12:00:00+00:00")
#: A public id (ADR 0033) of an original the test bundle carries a detail document for.
ORIGINAL = "orig_" + "7" * 32


@dataclass(frozen=True)
class KeyPair:
    private: Ed25519PrivateKey
    private_path: Path
    public_path: Path


def make_keys(directory: Path, name: str = "dev") -> KeyPair:
    key = generate_private_key()
    private_path = directory / f"{name}.key"
    public_path = directory / f"{name}.pub"
    private_path.write_bytes(private_key_pem(key))
    public_path.write_bytes(public_key_pem(key.public_key()))
    return KeyPair(key, private_path, public_path)


def document(body: str, status: int = 200) -> dict[str, Any]:
    return {"status": status, "content_type": "application/json; charset=utf-8", "body": body}


def bundle_payload(
    organisation_id: int, *, as_of: datetime = NOW, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": FORMAT_BUNDLE,
        "schema_version": SCHEMA_VERSION,
        "organisation_id": organisation_id,
        "as_of": as_of,
        "expires_at": as_of + CUSTOMER_CACHE_MAX_STALE,
        "api_version": "1",
        "documents": {
            "originals": document(f'[{{"org": {organisation_id}, "as_of": "{as_of}"}}]'),
            f"originals/{ORIGINAL}": document('{"original": 7}'),
            "originals/*": document('{"detail": "Original not found"}', status=404),
        },
        "page_ids": [],
    }
    payload.update(overrides)
    return payload


def bundle_wire(
    key: Ed25519PrivateKey, organisation_id: int, *, as_of: datetime = NOW, **overrides: Any
) -> bytes:
    return seal(bundle_payload(organisation_id, as_of=as_of, **overrides), key)


def purge_wire(
    key: Ed25519PrivateKey,
    *,
    mode: str = "delete",
    organisation_ids: tuple[int, ...] = (),
    page_ids: tuple[int, ...] = (),
    issued_at: datetime = NOW,
    nonce: str = "nonce-0123456789abcdef",
) -> bytes:
    return seal(
        {
            "format": FORMAT_PURGE,
            "schema_version": SCHEMA_VERSION,
            "mode": mode,
            "organisation_ids": list(organisation_ids),
            "page_ids": list(page_ids),
            "issued_at": issued_at,
            "nonce": nonce,
        },
        key,
    )


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)
