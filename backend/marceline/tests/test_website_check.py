"""The two new `website/check.py` guards refuse what they claim to refuse (issues #124, #128).

`website/check.py` is the whole build for the public marketing site (`make website-check`, run
inside `make check`). It is standard-library only and has no test of its own beyond the internal
canaries it runs on itself, so — same idea as `test_prod_compose_invariants.py` for the production
compose checker — this walks it from the outside: one call that must be accepted (today's real
site), and one mutation per rule that must be refused.

Loaded from its path rather than imported: `website/check.py` is outside every Python package this
suite knows about (`website/` has no `__init__.py` and is not part of `backend/`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WEBSITE_CHECK = REPO_ROOT / "website" / "check.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("website_check", WEBSITE_CHECK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # check.py's dataclasses are defined under `from __future__ import annotations`; resolving
    # their fields needs the module already registered in sys.modules while it executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


def _page(**kwargs: object) -> Any:
    defaults: dict[str, object] = {
        "path": check.PUBLIC / "index.html",
        "url": "/",
        "lang": "de",
    }
    defaults.update(kwargs)
    page: Any = check.Page(**defaults)
    return page


# ---------------------------------------------------------------------- the real site is accepted


def test_the_real_site_passes_both_new_guards() -> None:
    """Without this the tests below would pass on a checker that refuses everything."""
    assert check.main() == 0


def test_forbidden_content_check_is_silent_on_the_real_pages() -> None:
    pages = [
        check.Page(path=p, url=check.url_for(p), lang="")
        for p in sorted(check.PUBLIC.rglob("*.html"))
    ]
    for page in pages:
        parser = check.PageParser(page, check.Problems())
        parser.feed(page.path.read_text(encoding="utf-8"))
        parser.close()

    problems = check.Problems()
    check.check_forbidden_content(pages, problems)
    assert problems.items == []


def test_cloud_flag_check_is_silent_against_the_real_repository() -> None:
    pages = [
        check.Page(path=p, url=check.url_for(p), lang="")
        for p in sorted(check.PUBLIC.rglob("*.html"))
    ]
    for page in pages:
        parser = check.PageParser(page, check.Problems())
        parser.feed(page.path.read_text(encoding="utf-8"))
        parser.close()

    defaults = check.cloud_flag_defaults(REPO_ROOT)
    assert defaults, "cloud_flag_defaults() found nothing to check at all"
    assert all(value is False for _, value in defaults), (
        f"CLOUD_FINGERPRINTING_ENABLED is not false everywhere: {defaults} — either the flag "
        "moved (see issue #124 first) or cloud_flag_defaults() needs updating"
    )

    problems = check.Problems()
    check.check_cloud_flag_tied_to_claim(pages, defaults, problems)
    assert problems.items == []


# ------------------------------------------------------------------------------ issue #128 guards


def test_a_bare_domain_in_prose_is_refused() -> None:
    page = _page(text="besuche videos.example jetzt")
    problems = check.Problems()
    check.check_forbidden_content([page], problems)
    assert any("domain-shaped" in item for item in problems.items)


def test_our_own_domain_is_not_refused() -> None:
    page = _page(text="Schreib uns unter contact@marceline.studio oder auf marceline.studio")
    problems = check.Problems()
    check.check_forbidden_content([page], problems)
    assert problems.items == []


def test_a_supervisory_authoritys_email_address_is_not_refused() -> None:
    """The real Art. 13 notice prints a data-protection authority's address; it must never be
    mistaken for a site name — the whole point of stripping email-shaped text first."""
    page = _page(text="poststelle@aufsicht.example.de")
    problems = check.Problems()
    check.check_forbidden_content([page], problems)
    assert problems.items == []


def test_a_local_file_reference_is_not_refused() -> None:
    """The Impressum's font-licence link text ("LICENSES.txt") must not read as a domain."""
    page = _page(text="Lizenztext und Urhebervermerke: LICENSES.txt")
    problems = check.Problems()
    check.check_forbidden_content([page], problems)
    assert problems.items == []


def test_a_quoted_title_is_refused() -> None:
    page = _page(text='der Titel "Beispielvideo" wurde gefunden')
    problems = check.Problems()
    check.check_forbidden_content([page], problems)
    assert any("quotation mark" in item for item in problems.items)


def test_a_possessive_apostrophe_is_not_refused() -> None:
    page = _page(lang="en", text="the server's operational messages")
    problems = check.Problems()
    check.check_forbidden_content([page], problems)
    assert problems.items == []


@pytest.mark.parametrize("tag", ["blockquote", "cite", "q"])
def test_testimonial_markup_is_refused(tag: str) -> None:
    problems = check.Problems()
    parser = check.PageParser(_page(), problems)
    parser.feed(f"<{tag}>a happy customer</{tag}>")
    parser.close()
    assert any(f"<{tag}>" in item for item in problems.items)


def test_forbidden_content_canaries_pass_against_the_real_patterns() -> None:
    problems = check.Problems()
    check.check_forbidden_content_canaries(problems)
    assert problems.items == []


def test_forbidden_content_canaries_catch_a_weakened_domain_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never_matches: Any = check.re.compile(r"(?!)")
    monkeypatch.setattr(check, "DOMAIN_SHAPE", never_matches)
    problems = check.Problems()
    check.check_forbidden_content_canaries(problems)
    assert any("bare domain" in item and "no longer catches" in item for item in problems.items)


# ------------------------------------------------------------------------------ issue #124 guards


def test_a_true_default_with_the_claim_still_on_the_page_is_refused() -> None:
    page = _page(text="Dein Video bleibt bei dir.")
    problems = check.Problems()
    check.check_cloud_flag_tied_to_claim([page], [("fake/settings.py", True)], problems)
    assert len(problems.items) == 1
    assert "fake/settings.py" in problems.items[0]
    assert "issue #124" in problems.items[0]


def test_a_true_default_with_the_english_claim_is_also_refused() -> None:
    page = _page(lang="en", text="Your video stays with you.")
    problems = check.Problems()
    check.check_cloud_flag_tied_to_claim([page], [("fake/settings.py", True)], problems)
    assert len(problems.items) == 1


def test_a_true_default_is_accepted_once_the_claim_is_gone() -> None:
    """Proves the sequencing the issue asks for: change the wording first, then the flag may move.
    Does not exercise changing the real website copy — that stays the operator's call."""
    page = _page(text="a conditional sentence that names no promise about where the video stays")
    problems = check.Problems()
    check.check_cloud_flag_tied_to_claim([page], [("fake/settings.py", True)], problems)
    assert problems.items == []


def test_a_false_default_with_the_claim_still_on_the_page_is_accepted() -> None:
    page = _page(text="Dein Video bleibt bei dir.")
    problems = check.Problems()
    check.check_cloud_flag_tied_to_claim([page], [("fake/settings.py", False)], problems)
    assert problems.items == []


def test_an_unparseable_default_is_refused_not_silently_skipped() -> None:
    page = _page(text="nothing about the claim here")
    problems = check.Problems()
    check.check_cloud_flag_tied_to_claim([page], [("fake/settings.py", None)], problems)
    assert len(problems.items) == 1
    assert "could not find" in problems.items[0]


# ------------------------------------------- issue #124: the file parser (cloud_flag_defaults)


def _write_repo(
    tmp_path: Path, *, settings_default: str, compose_fallback: str, prod_value: str
) -> Path:
    settings_dir = tmp_path / "backend" / "marceline"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.py").write_text(
        "CLOUD_FINGERPRINTING_ENABLED = env_bool(\n"
        '    "CLOUD_FINGERPRINTING_ENABLED", default=' + settings_default + "\n)\n"
    )

    infra_dir = tmp_path / "infra"
    infra_dir.mkdir()
    compose_line = (
        "      CLOUD_FINGERPRINTING_ENABLED: "
        "${CLOUD_FINGERPRINTING_ENABLED:-" + compose_fallback + "}\n"
    )
    (infra_dir / "compose.yaml").write_text(compose_line + compose_line)

    prod_dir = infra_dir / "prod"
    prod_dir.mkdir()
    # Two occurrences since ADR 0044's amendment of 2026-09-19: the shared anchor states the
    # refusal, and `web` — the one service that may relay an upload — takes it from the environment
    # with a default of its own. Both shapes have to be read, and both have to be reported.
    (prod_dir / "compose.yaml").write_text(
        '  CLOUD_FINGERPRINTING_ENABLED: "' + prod_value + '"\n'
        "      CLOUD_FINGERPRINTING_ENABLED: ${CLOUD_FINGERPRINTING_ENABLED:-" + prod_value + "}\n"
    )
    return tmp_path


def test_cloud_flag_defaults_reads_a_healthy_repository(tmp_path: Path) -> None:
    repo = _write_repo(
        tmp_path, settings_default="False", compose_fallback="false", prod_value="false"
    )
    findings = check.cloud_flag_defaults(repo)
    assert [value for _, value in findings] == [False, False, False, False, False]


def test_cloud_flag_defaults_reads_a_flipped_settings_default(tmp_path: Path) -> None:
    repo = _write_repo(
        tmp_path, settings_default="True", compose_fallback="false", prod_value="false"
    )
    findings = dict(check.cloud_flag_defaults(repo))
    assert findings["backend/marceline/settings.py"] is True
    assert findings["infra/prod/compose.yaml (occurrence 1)"] is False


def test_cloud_flag_defaults_reads_a_flipped_compose_fallback(tmp_path: Path) -> None:
    repo = _write_repo(
        tmp_path, settings_default="False", compose_fallback="true", prod_value="false"
    )
    findings = dict(check.cloud_flag_defaults(repo))
    assert findings["infra/compose.yaml (occurrence 1)"] is True
    assert findings["infra/compose.yaml (occurrence 2)"] is True
    assert findings["backend/marceline/settings.py"] is False


def test_cloud_flag_defaults_reads_a_flipped_prod_value(tmp_path: Path) -> None:
    repo = _write_repo(
        tmp_path, settings_default="False", compose_fallback="false", prod_value="true"
    )
    findings = dict(check.cloud_flag_defaults(repo))
    assert findings["infra/prod/compose.yaml (occurrence 1)"] is True
    assert findings["infra/prod/compose.yaml (occurrence 2)"] is True


def test_cloud_flag_defaults_sees_a_second_production_service_that_defaults_to_true(
    tmp_path: Path,
) -> None:
    """The relay's shape (ADR 0044 amendment 2026-09-19) writes the flag twice in the production
    file, so the guard must report **every** occurrence: a `re.search` would have stopped at the
    anchor's honest `"false"` and never seen a service that defaults to true behind it — which is
    exactly the change issue #124's guard exists to catch."""
    repo = _write_repo(
        tmp_path, settings_default="False", compose_fallback="false", prod_value="false"
    )
    prod = repo / "infra" / "prod" / "compose.yaml"
    prod.write_text(
        prod.read_text()
        + "      CLOUD_FINGERPRINTING_ENABLED: ${CLOUD_FINGERPRINTING_ENABLED:-true}\n"
    )

    findings = dict(check.cloud_flag_defaults(repo))

    assert findings["infra/prod/compose.yaml (occurrence 3)"] is True
    page = _page(text="Dein Video bleibt bei dir.")
    problems = check.Problems()
    check.check_cloud_flag_tied_to_claim([page], list(findings.items()), problems)
    assert len(problems.items) == 1
    assert "occurrence 3" in problems.items[0]


def test_an_empty_production_default_is_read_as_false(tmp_path: Path) -> None:
    """`${VAR:-}` is an empty string, which `env_bool` reads as the code default — false. It must
    not read as "unparseable" (that would fail the check for no reason) nor as true."""
    repo = _write_repo(
        tmp_path, settings_default="False", compose_fallback="false", prod_value="false"
    )
    prod = repo / "infra" / "prod" / "compose.yaml"
    prod.write_text("      CLOUD_FINGERPRINTING_ENABLED: ${CLOUD_FINGERPRINTING_ENABLED:-}\n")

    findings = dict(check.cloud_flag_defaults(repo))

    assert findings["infra/prod/compose.yaml (occurrence 1)"] is False


def test_cloud_flag_defaults_fails_closed_when_a_file_cannot_be_parsed(tmp_path: Path) -> None:
    settings_dir = tmp_path / "backend" / "marceline"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.py").write_text("# the setting moved elsewhere\n")
    infra_dir = tmp_path / "infra"
    infra_dir.mkdir()
    (infra_dir / "compose.yaml").write_text("services:\n  web:\n    image: x\n")
    prod_dir = infra_dir / "prod"
    prod_dir.mkdir()
    (prod_dir / "compose.yaml").write_text("services:\n  web:\n    image: x\n")

    findings = check.cloud_flag_defaults(tmp_path)
    assert all(value is None for _, value in findings), findings


# ------------------------------------------------------------------------------- issue #145 guard


@pytest.mark.parametrize(
    ("description", "text"),
    [
        ("the symbol", "Marceline® findet Kopien"),
        ("an English claim", "a registered trademark of the provider"),
        ("a German claim", "der Name ist eine eingetragene Marke"),
        ("a German protection claim", "der Name ist markenrechtlich geschützt"),
    ],
)
def test_a_registered_trademark_claim_is_refused(description: str, text: str) -> None:
    page = _page(text=text)
    problems = check.Problems()
    check.check_registration_claim([page], problems)
    assert any("registered-trademark claim" in item for item in problems.items), description


def test_a_trademark_symbol_without_registration_is_allowed() -> None:
    """`™` asserts nothing and is the mark to use until a registration exists (issue #145)."""
    page = _page(text="Marceline™ findet Kopien")
    problems = check.Problems()
    check.check_registration_claim([page], problems)
    assert problems.items == []


@pytest.mark.parametrize("text", check.REGISTRATION_CLAIM_ALLOWED)
def test_a_commercial_register_sentence_is_not_refused(text: str) -> None:
    """The Impressum must be able to say the provider is not in the register (§ 5 DDG)."""
    page = _page(text=text)
    problems = check.Problems()
    check.check_registration_claim([page], problems)
    assert problems.items == []


def test_registration_claim_check_is_silent_on_the_real_pages() -> None:
    pages = [
        check.Page(path=p, url=check.url_for(p), lang="")
        for p in sorted(check.PUBLIC.rglob("*.html"))
    ]
    for page in pages:
        parser = check.PageParser(page, check.Problems())
        parser.feed(page.path.read_text(encoding="utf-8"))
        parser.close()

    problems = check.Problems()
    check.check_registration_claim(pages, problems)
    assert problems.items == []


def test_registration_claim_canaries_pass_against_the_real_patterns() -> None:
    problems = check.Problems()
    check.check_registration_claim_canaries(problems)
    assert problems.items == []


def test_registration_claim_canaries_catch_a_deleted_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emptying the list to get a green check is the failure this guards against."""
    monkeypatch.setattr(check, "REGISTRATION_CLAIM_WORDING", {"en": []})
    problems = check.Problems()
    check.check_registration_claim_canaries(problems)
    assert len(problems.items) == len(check.REGISTRATION_CLAIM_CANARIES)
    assert all("no longer catches" in item for item in problems.items)


def test_registration_claim_canaries_catch_a_pattern_that_is_too_wide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pattern that also eats the Impressum's own register sentence is reported too."""
    monkeypatch.setattr(
        check,
        "REGISTRATION_CLAIM_WORDING",
        {**check.REGISTRATION_CLAIM_WORDING, "de": [r"eingetragen"]},
    )
    problems = check.Problems()
    check.check_registration_claim_canaries(problems)
    assert any("now also refuses" in item for item in problems.items)
