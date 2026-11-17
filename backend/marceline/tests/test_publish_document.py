"""`website/publish_document.py` prints an entry the invite flow accepts, over the right bytes.

The script is the only place the `ONBOARDING_DOCUMENTS` digest is ever computed
(`docs/runbooks/onboarding-documents.md`), and nothing in the backend fetches the URL to check it
(`accounts.onboarding` parses the entry, `accounts.invites.record_acceptances` stores what it
says). So the one thing that must be true is tested from both ends here: the digest is the SHA-256
of exactly the bytes a server hands out, and the line the script prints is a line
`accounts.onboarding.published_documents()` accepts.

Loaded from its path, like `test_website_check.py` does with `website/check.py`: `website/` is not
a package and not part of `backend/`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType

import pytest
from django.test import override_settings

from accounts import onboarding

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "website" / "publish_document.py"

#: A page with nothing left open, as the runbook's filled copy looks: a version it names, no
#: placeholder, no draft marker.
PUBLISHED_PAGE = """<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><title>Pilotvereinbarung — Marceline</title></head>
<body><main><h1>Pilotvereinbarung</h1><p>Fassung 2026-10-01 vom 1. Oktober 2026.</p></main></body>
</html>
"""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("publish_document", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load()


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = publish.main(list(argv))
    return code, out.getvalue().strip(), err.getvalue()


#: Any URL on our own origin; which path it is does not matter to the digest.
SOME_URL = "https://marceline.studio/dokumente/pilotvereinbarung-2026-10-01"


def _entry(
    path: Path,
    *,
    document_id: str = "pilot_agreement",
    version: str = "2026-10-01",
    url: str = SOME_URL,
) -> tuple[int, str, str]:
    """One publish run, with the arguments a real one takes."""
    return _run(str(path), "--id", document_id, "--version", version, "--url", url)


@pytest.fixture
def page(tmp_path: Path) -> Path:
    path = tmp_path / "pilotvereinbarung-2026-10-01.html"
    path.write_text(PUBLISHED_PAGE, encoding="utf-8")
    return path


# ------------------------------------------------------- the digest is over the served bytes


def test_digest_is_sha256_of_the_file_bytes(page: Path) -> None:
    """No normalisation of any kind: Caddy's file_server hands out this file unchanged."""
    code, entry, _ = _entry(page)
    assert code == 0
    assert entry.split(":")[2] == hashlib.sha256(page.read_bytes()).hexdigest()


def test_a_single_byte_changes_the_digest(page: Path) -> None:
    """Which is the whole point of storing it: a page edited after publication is a new version."""
    code, first, _ = _entry(page, url="https://marceline.studio/d/x")
    assert code == 0
    page.write_bytes(page.read_bytes() + b"\n")
    code, second, _ = _entry(page, url="https://marceline.studio/d/x")
    assert code == 0
    assert first != second


# ------------------------------------------------------- the backend accepts what it prints


def test_the_printed_entry_is_what_the_backend_parses(page: Path) -> None:
    code, entry, _ = _entry(page)
    assert code == 0
    parsed = onboarding._parse_entry(entry)
    assert parsed.document_id == "pilot_agreement"
    assert parsed.version == "2026-10-01"
    assert parsed.sha256 == hashlib.sha256(page.read_bytes()).hexdigest()
    assert parsed.url == "https://marceline.studio/dokumente/pilotvereinbarung-2026-10-01"


def test_two_entries_satisfy_published_documents(tmp_path: Path, page: Path) -> None:
    """The end of the runbook: both entries, comma-separated, and the accept step stops refusing."""
    notice = tmp_path / "datenschutzhinweise-dienst-2026-10-01.html"
    notice.write_text(PUBLISHED_PAGE.replace("Pilotvereinbarung", "Datenschutzhinweise"), "utf-8")

    entries = []
    for path, document_id in ((page, "pilot_agreement"), (notice, "service_privacy_notice")):
        code, entry, _ = _entry(
            path,
            document_id=document_id,
            url=f"https://marceline.studio/dokumente/{path.stem}",
        )
        assert code == 0
        entries.append(entry)

    with override_settings(ONBOARDING_DOCUMENTS=",".join(entries)):
        documents = onboarding.published_documents()
        assert onboarding.documents_available() is True
    assert [document.document_id for document in documents] == list(onboarding.REQUIRED_DOCUMENTS)
    assert documents[0].sha256 == hashlib.sha256(page.read_bytes()).hexdigest()
    assert documents[1].sha256 == hashlib.sha256(notice.read_bytes()).hexdigest()


def test_the_script_knows_exactly_the_backend_document_ids() -> None:
    """A rename on one side and not the other would be found here, not by a refused redemption."""
    assert publish.KNOWN_DOCUMENT_IDS == onboarding.REQUIRED_DOCUMENTS


# ------------------------------------------------------------------------------ the refusals


@pytest.mark.parametrize(
    ("marker", "line"),
    [
        ("placeholder", "<p>Anbieter ist {{ANBIETER}}.</p>"),
        ("counsel marker", "<p>[ANWALT] offen.</p>"),
        ("platzhalter note", "<p>PLATZHALTER: eine Seite.</p>"),
        ("todo", "<p>TODO(hoster)</p>"),
        ("draft banner", "<p>Entwurf — nicht anwaltlich geprüft</p>"),
        ("render marker", "<p>RENDERN:</p>"),
    ],
)
def test_a_draft_is_refused(tmp_path: Path, marker: str, line: str) -> None:
    path = tmp_path / "draft.html"
    path.write_text(PUBLISHED_PAGE.replace("</main>", f"{line}</main>"), encoding="utf-8")
    code, out, err = _entry(path, url="https://marceline.studio/d/x")
    assert code == 1, marker
    assert out == ""
    assert "refused" in err


def test_a_page_that_does_not_name_its_version_is_refused(page: Path) -> None:
    code, _, err = _entry(page, version="2026-11-01", url="https://marceline.studio/d/x")
    assert code == 1
    assert "does not name the version" in err


@pytest.mark.parametrize("version", ["2026:10:01", "a,b", "with space", "x" * 41])
def test_a_version_that_would_break_the_entry_is_refused(page: Path, version: str) -> None:
    code, _, err = _entry(page, version=version, url="https://marceline.studio/d/x")
    assert code == 1
    assert "refused" in err


def test_an_empty_version_is_a_usage_error(page: Path) -> None:
    """Not a refusal but argparse's own exit: there is nothing to check yet."""
    with pytest.raises(SystemExit) as exit_info:
        _run(str(page), "--id", "pilot_agreement", "--version", "")
    assert exit_info.value.code == 2


def test_an_unknown_document_id_is_refused(page: Path) -> None:
    code, _, err = _entry(page, document_id="terms_of_service")
    assert code == 1
    assert "unknown document id" in err


def test_a_foreign_origin_is_refused(page: Path) -> None:
    code, _, err = _entry(page, url="https://example.invalid/d/x")
    assert code == 1
    assert "not on https://marceline.studio" in err


def test_a_file_outside_public_needs_an_explicit_url(page: Path) -> None:
    """The drafts live outside `website/public/`, where no URL can be derived — on purpose."""
    code, _, err = _run(str(page), "--id", "pilot_agreement", "--version", "2026-10-01")
    assert code == 1
    assert "cannot be derived" in err


# --------------------------------------------------------------------------------- --check


def test_check_confirms_an_unchanged_page(page: Path) -> None:
    code, entry, _ = _entry(page, url="https://marceline.studio/d/x")
    assert code == 0
    code, out, err = _run(str(page), "--check", entry)
    assert code == 0
    assert out == ""
    assert "still matches" in err


def test_check_refuses_after_the_page_changed(page: Path) -> None:
    code, entry, _ = _entry(page, url="https://marceline.studio/d/x")
    assert code == 0
    page.write_text(PUBLISHED_PAGE.replace("Oktober", "November"), encoding="utf-8")
    code, _, err = _run(str(page), "--check", entry)
    assert code == 1
    assert "does not match the file any more" in err


# ------------------------------------------------------- the two unpublished drafts are drafts


def test_the_drafts_in_the_repository_are_refused_as_they_stand() -> None:
    """`website/drafts/` is where a document waits: publishing one is the operator's decision."""
    drafts = sorted((REPO_ROOT / "website" / "drafts").glob("*.html"))
    assert len(drafts) == 2
    for path in drafts:
        code, out, err = _entry(path, url="https://marceline.studio/d/x")
        assert code == 1, path.name
        assert out == ""
        assert "refused" in err
