"""Files the operator has permanently decided never to import.

An accidental re-upload of the same file must import nothing, so this is a property of
`import_fingerprints` itself (`IGNORED_FILES`), not a one-off decision made once at the terminal.
"""

import io
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog.models import Creator
from marceline.management.commands.import_fingerprints import IGNORED_FILES, Ignored, ignored_reason
from marceline.models import OriginalOwnership
from matching.models import Reference
from matching.tests.test_fingerprint import synthetic, write

pytestmark = pytest.mark.django_db


def call(*args: str) -> str:
    output = io.StringIO()
    try:
        call_command("import_fingerprints", *args, stdout=output)
    except CommandError as exc:
        raise CommandError(f"{exc}\n{output.getvalue()}") from exc
    return output.getvalue()


@pytest.fixture
def creator() -> Creator:
    return Creator.objects.create(name="Synthetic Creator", aliases=["Alias"])


@pytest.fixture
def ignored(monkeypatch: pytest.MonkeyPatch) -> Ignored:
    """One throwaway ignore entry, isolated from the real, checked-in list."""
    entry = Ignored(sha256="ab" * 32, reason="test: cast never settled")
    monkeypatch.setattr("marceline.management.commands.import_fingerprints.IGNORED_FILES", (entry,))
    return entry


def test_the_real_entry_matches_by_its_documented_prefix() -> None:
    """ "Sample Title 6": its full sha256 was never recovered (2026-09-17), so it is recorded as
    a 12-character prefix. A full digest starting with that prefix is still ignored."""
    entry = next(item for item in IGNORED_FILES if "Sample Title 6" in item.reason)
    assert len(entry.sha256) < 64, "documented as a prefix, not a full digest"

    padded = entry.sha256 + "0" * (64 - len(entry.sha256))
    assert ignored_reason(padded) == entry.reason
    assert ignored_reason("f" * 64) is None


def test_an_ignored_file_is_skipped_with_its_reason(tmp_path: Path, ignored: Ignored) -> None:
    path = write(tmp_path / "one.json", synthetic(sha256=ignored.sha256))

    output = call(str(path))

    assert f"ignored: {ignored.reason}" in output
    assert "imported 0, ignored 1, errors 0" in output


def test_an_ignored_file_writes_nothing(tmp_path: Path, ignored: Ignored) -> None:
    path = write(tmp_path / "one.json", synthetic(sha256=ignored.sha256))

    call(str(path))

    assert not Reference.objects.exists()
    assert not OriginalOwnership.objects.exists()


def test_dry_run_reports_the_same_ignore(tmp_path: Path, ignored: Ignored) -> None:
    path = write(tmp_path / "one.json", synthetic(sha256=ignored.sha256))

    output = call("--dry-run", str(path))

    assert f"ignored: {ignored.reason}" in output
    assert "would import 0, ignored 1, errors 0" in output
    assert not Reference.objects.exists()


def test_compare_on_an_ignored_file_says_ignored_instead(tmp_path: Path, ignored: Ignored) -> None:
    path = write(tmp_path / "one.json", synthetic(sha256=ignored.sha256))

    output = call("--compare", str(path))

    assert f"ignored: {ignored.reason}" in output
    assert "compare with reference" not in output


def test_the_rest_of_a_multi_file_run_still_imports(
    tmp_path: Path, ignored: Ignored, creator: Creator
) -> None:
    write(tmp_path / "ignored.json", synthetic(sha256=ignored.sha256))
    write(tmp_path / "ok.json", synthetic(sha256="cd" * 32))

    output = call(str(tmp_path))

    assert "imported 1, ignored 1, errors 0" in output
    assert Reference.objects.count() == 1
    assert Reference.objects.get().sha256 == "cd" * 32
