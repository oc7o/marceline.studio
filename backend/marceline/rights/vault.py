"""Encrypted storage for proof documents (ADR 0034): contract excerpts, mandates, schedules.

- **Encrypted at rest** with Fernet (AES-128-CBC + HMAC-SHA256, `cryptography`). Keys come from
  `RIGHTS_VAULT_KEYS` only — comma-separated, the first encrypts, all decrypt (rotation). No key
  is ever in the repository; tests generate one.
- **Outside every served directory.** `RIGHTS_VAULT_DIR` must be an absolute path outside the
  repository (no static root, no frontend build, no mounted source tree) — checked by
  `core.checkout_paths.inside_checkout`, the rule shared with the upload spool
  (`marceline.uploads.spool`), so a container's bind mount is not mistaken for "everywhere is the
  repository" (issue #59, found alongside the same bug in the spool during the 2026-09-18 trial).
  Files are named by a random token, never by anything the customer sent, and written with mode
  0600 in a 0700 directory.
- **Documents, not media.** PDF, PNG and JPEG only, recognised by their leading bytes (the name and
  the declared type are ignored), at most `RIGHTS_PROOF_MAX_BYTES`.
- **Integrity.** The plaintext's sha256 is stored on the row and checked on every read, so a file
  swapped between rows, or tampered with, is refused.

Without both settings the vault is unavailable and uploads are refused; nothing falls back to
plaintext or to a default location.
"""

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings

from core.checkout_paths import inside_checkout

#: Leading bytes → content type. Anything else is refused.
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)
CONTENT_TYPES = tuple(content_type for _, content_type in SIGNATURES)
STORAGE_NAME = re.compile(r"^[0-9a-f]{48}$")
SUFFIX = ".bin"


class VaultError(Exception):
    """A refusal with a machine-readable code (`unavailable`, `too_large`, `unsupported_type`,
    `empty`, `missing`, `corrupt`)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class StoredBlob:
    storage_name: str
    sha256: str
    size_bytes: int
    content_type: str


def repository_root() -> Path:
    return Path(settings.BASE_DIR).resolve().parent


def problems() -> list[str]:
    """Why the vault cannot be used, or an empty list."""
    found = []
    keys = [key.strip() for key in settings.RIGHTS_VAULT_KEYS.split(",") if key.strip()]
    if not keys:
        found.append("RIGHTS_VAULT_KEYS is not set")
    else:
        try:
            [Fernet(key) for key in keys]
        except ValueError:
            found.append("RIGHTS_VAULT_KEYS holds a value that is not a Fernet key")
    raw = settings.RIGHTS_VAULT_DIR
    if not raw:
        found.append("RIGHTS_VAULT_DIR is not set")
    else:
        directory = Path(raw)
        if not directory.is_absolute():
            found.append("RIGHTS_VAULT_DIR must be an absolute path")
        else:
            resolved = directory.resolve()
            code_dir = Path(settings.BASE_DIR).resolve()
            if inside_checkout(resolved, code_dir=code_dir, checkout_root=repository_root()):
                found.append("RIGHTS_VAULT_DIR must be outside the repository")
    return found


def available() -> bool:
    return not problems()


def _cipher() -> MultiFernet:
    found = problems()
    if found:
        raise VaultError("unavailable", "; ".join(found))
    keys = [key.strip() for key in settings.RIGHTS_VAULT_KEYS.split(",") if key.strip()]
    return MultiFernet([Fernet(key) for key in keys])


def _directory() -> Path:
    directory = Path(settings.RIGHTS_VAULT_DIR)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _path(storage_name: str) -> Path:
    if not STORAGE_NAME.match(storage_name):
        raise VaultError("missing", "not a storage name")
    return _directory() / f"{storage_name}{SUFFIX}"


def sniff(data: bytes) -> str | None:
    """The content type these bytes start like, or None."""
    for signature, content_type in SIGNATURES:
        if data.startswith(signature):
            return content_type
    return None


def check_upload(data: bytes) -> str:
    """The content type of an acceptable upload, or a `VaultError`."""
    if not data:
        raise VaultError("empty")
    if len(data) > settings.RIGHTS_PROOF_MAX_BYTES:
        raise VaultError("too_large")
    content_type = sniff(data)
    if content_type is None:
        raise VaultError("unsupported_type")
    return content_type


def store(data: bytes) -> StoredBlob:
    """Encrypt and write one document; returns what the row must record."""
    content_type = check_upload(data)
    cipher = _cipher()
    storage_name = secrets.token_hex(24)
    path = _path(storage_name)
    temporary = path.with_suffix(".tmp")
    token = cipher.encrypt(data)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return StoredBlob(
        storage_name=storage_name,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        content_type=content_type,
    )


def read(storage_name: str, expected_sha256: str) -> bytes:
    """Decrypt one document and check it is the one the row describes."""
    cipher = _cipher()
    path = _path(storage_name)
    try:
        token = path.read_bytes()
    except FileNotFoundError as error:
        raise VaultError("missing") from error
    try:
        data = cipher.decrypt(token)
    except InvalidToken as error:
        raise VaultError("corrupt", "cannot decrypt: wrong key or damaged file") from error
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise VaultError("corrupt", "digest does not match the record")
    return data


def delete(storage_name: str) -> bool:
    """Remove one stored document. False when it was already gone."""
    path = _path(storage_name)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
