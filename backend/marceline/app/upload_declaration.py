"""The declaration a customer makes before sending a file (issue #59, ADR 0044 amendment of
2026-09-19).

**DRAFT WORDING.** The texts in `upload_declaration.json` are a draft and must be reviewed before
`CLOUD_FINGERPRINTING_ENABLED` is switched on for real customers.

Why it exists, in one paragraph. A recording of a person can contain sensitive personal data, and
we cannot obtain consent from people in a file we have no relationship with. So the feature is
narrow: an upload is accepted **only** where the uploader declares they are the sole person in the
file, and gives explicit Art. 9(2)(a) consent for the hashing of it. That keeps the processing to
the uploader's own data. A file with other people is not refused as a product — it is pointed
at the local, in-browser route, where the bytes never leave the machine and we are not processing
anyone's data at all.

Same rules as every other declaration in this codebase (`marceline/rights/declarations.py`,
`accounts/declarations.py`):

- **A new wording is a new version, never an edit.** `MediaUpload` stores the version, the language,
  the timestamp **and a verbatim snapshot** of the text, so the words a person ticked can still be
  read years later whatever this file says by then.
- **Two statements, two ticks.** A warranty about other people and an Art. 9 consent are different
  kinds of thing; one checkbox for both would make the consent less than explicit.
- **Factual, and no duration.** `no_duration_promise` is asserted by a test, because the six-hour
  TTL is a crash backstop and not a schedule (ADR 0044 § 3 (d), (e)).
- The **German text is the operative one**; the English is a reading aid. Both are first person: the
  customer speaks about their own affairs, which is also why a declaration may state a proposition
  no string of ours ever may.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

_DATA = json.loads((Path(__file__).with_name("upload_declaration.json")).read_text("utf-8"))

#: The two statements, in the order a screen shows them.
STATEMENTS: Final[tuple[str, ...]] = ("sole_performer", "article9_consent")
#: The operative language of the declaration; the other is a reading aid.
OPERATIVE_LANGUAGE: Final[str] = "de"
#: The version a new upload must name.
CURRENT_VERSION: Final[str] = str(_DATA["current"])
#: Every version's statements per language, kept for the record after a newer one replaced it.
TEXTS: Final[dict[str, dict[str, dict[str, str]]]] = {
    str(version): {
        str(language): {str(key): str(text) for key, text in statements.items()}
        for language, statements in languages.items()
    }
    for version, languages in _DATA["versions"].items()
}
LANGUAGES: Final[frozenset[str]] = frozenset(TEXTS[CURRENT_VERSION])

#: Patterns a declaration may never contain: a promise about how long something takes, a guarantee,
#: an expectation or a likelihood. The same bans `frontend/src/i18n/wording.test.ts` applies to the
#: app's own words, applied here because these words are the server's and that test cannot see them.
BANNED_PATTERNS: Final[tuple[str, ...]] = (
    r"innerhalb (von )?\d+ (Stunden?|Tagen?)",
    r"within \d+ (hours?|days?)",
    r"h(ö|oe)chstens nach \d+",
    r"no later than \d+",
    r"garant",
    r"guarantee",
    r"wahrscheinlich",
    r"likely",
)


def texts_of(version: str, language: str) -> dict[str, str] | None:
    """The two statements a stored declaration refers to, or None for an unknown pair."""
    return TEXTS.get(version, {}).get(language)


def snapshot(version: str, language: str) -> str:
    """The verbatim text to store with one upload: both statements, in shown order, one per line.

    Stored rather than referenced, because a version's file could be lost and a record of consent
    that cannot be read is not a record. Raises `KeyError` for an unknown version or language, which
    the endpoint has already refused before it gets here.
    """
    statements = TEXTS[version][language]
    return "\n\n".join(statements[key] for key in STATEMENTS)


def banned_in(text: str) -> list[str]:
    """Which of the banned patterns this text contains. Empty is the only acceptable answer."""
    return [pattern for pattern in BANNED_PATTERNS if re.search(pattern, text, re.IGNORECASE)]
