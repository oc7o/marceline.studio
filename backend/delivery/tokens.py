"""The scoped service token production presents to the development export API (ADR 0028)."""

import hashlib
import hmac
import secrets

from delivery.models import ServiceToken

TOKEN_PREFIX = "mdx_"


def digest_of(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(name: str, scope: str = ServiceToken.Scope.EXPORT) -> tuple[ServiceToken, str]:
    """Store a new token's digest; returns the row and the token, which is never stored."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ServiceToken.objects.create(name=name, scope=scope, digest=digest_of(token))
    return row, token


def token_is_valid(token: str, scope: str) -> bool:
    """Whether a presented token is a live token of this scope. Reads only (the export role may be
    `SELECT`-only on the database)."""
    if not token.startswith(TOKEN_PREFIX):
        return False
    digest = digest_of(token)
    stored = (
        ServiceToken.objects.filter(digest=digest, scope=scope, revoked_at__isnull=True)
        .values_list("digest", flat=True)
        .first()
    )
    return stored is not None and hmac.compare_digest(stored, digest)
