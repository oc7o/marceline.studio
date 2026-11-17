"""The rights declaration a customer makes when adding an original (issue #29).

The wording lives in `rights_confirmation.json`, one entry per version and language, and never
changes in place: a new wording is a new version, and `current` names the one the app shows. The
customer UI carries the same strings (`frontend/src/i18n`), and `addOriginal.test.ts` there reads
this file and fails when the two drift apart — so the version stored on a `RightsConfirmation`
always names the exact sentence the customer ticked.

Factual wording only: an original, the rights in it. Never "leak" or "infringing" — the customer
declares something about their own work, not about anybody else's upload.

Version `2026-09-18` says *original* where `2026-09-17` said *video*: since issue #116 an original
is a video **or** a photo collection (ADR 0020, ADR 0053), and a declaration is the one string in
this product that has to be exactly true for the thing it is shown next to. One current sentence
for both kinds, not one per kind: `RightsConfirmation` records the exact sentence a person ticked,
and a second dimension on that record buys nothing a truthful single sentence does not.
"""

import json
from pathlib import Path
from typing import Final

_DATA = json.loads((Path(__file__).with_name("rights_confirmation.json")).read_text("utf-8"))

#: The version a new declaration must name.
CURRENT_VERSION: Final[str] = str(_DATA["current"])
#: Every version's text per language, kept for the record even after a newer one replaced it.
TEXTS: Final[dict[str, dict[str, str]]] = {
    str(version): {str(language): str(text) for language, text in texts.items()}
    for version, texts in _DATA["versions"].items()
}
LANGUAGES: Final[frozenset[str]] = frozenset(TEXTS[CURRENT_VERSION])


def text_of(version: str, language: str) -> str | None:
    """The sentence a stored declaration refers to, or None for an unknown pair."""
    return TEXTS.get(version, {}).get(language)
