from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from delivery.envelope import load_private_key, load_public_key
from delivery.models import ServiceToken
from delivery.tokens import create_token, digest_of, token_is_valid


@pytest.mark.django_db
def test_only_the_digest_is_stored_and_the_token_validates() -> None:
    row, token = create_token("prod")

    assert token not in row.digest
    assert row.digest == digest_of(token)
    assert token_is_valid(token, ServiceToken.Scope.EXPORT)


@pytest.mark.django_db
def test_revoked_unknown_and_wrong_scope_tokens_do_not_validate() -> None:
    row, token = create_token("prod")

    assert not token_is_valid(token + "x", ServiceToken.Scope.EXPORT)
    assert not token_is_valid(token, "purge")
    assert not token_is_valid(row.digest, ServiceToken.Scope.EXPORT)
    row.revoked_at = timezone.now()
    row.save()
    assert not token_is_valid(token, ServiceToken.Scope.EXPORT)


@pytest.mark.django_db
def test_create_service_token_prints_the_token_once() -> None:
    out = StringIO()
    call_command("create_service_token", "--name", "prod-1", stdout=out, stderr=StringIO())

    assert token_is_valid(out.getvalue().strip(), ServiceToken.Scope.EXPORT)
    with pytest.raises(CommandError):
        call_command("create_service_token", "--name", "prod-1", stdout=out, stderr=StringIO())


def test_generate_signing_key_writes_a_private_key_and_prints_only_the_public_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "signing.key"
    out = StringIO()

    call_command("generate_signing_key", "--private-key", str(path), stdout=out, stderr=StringIO())

    printed = out.getvalue()
    assert "PRIVATE" not in printed and "BEGIN PUBLIC KEY" in printed
    assert path.stat().st_mode & 0o777 == 0o600
    public_path = tmp_path / "signing.pub"
    public_path.write_text(printed)
    assert load_public_key(public_path) == load_private_key(path).public_key()
    with pytest.raises(CommandError, match="refusing to overwrite"):
        call_command("generate_signing_key", "--private-key", str(path), stdout=StringIO())
