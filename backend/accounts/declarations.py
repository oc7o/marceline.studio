"""The customer's own declarations, versioned: *Unternehmer* (issue #92) and majority (issue #123).

Same rule as `marceline/rights/declarations.py`: **a new wording is a new version, never an edit**.
A recorded declaration stores its version *and* a snapshot of the text, so the text a signature
covered can still be read years later, whatever this file says by then.

Unlike the rights declarations, this one is not shown by any screen. It is a clause of the pilot
agreement (issue #91), signed on paper or as a counter-signed PDF — there is no checkout and no
web form. What this
module gives is the exact wording staff record as having been signed.

**The German text is the operative one**; the English is a reading aid and is never signed on its
own. Two operative languages for one declaration would mean two things could have been agreed, and
the agreement is German.

The declaration is not, by itself, the point: a tick converts nobody. What carries weight is the
objective evidence recorded beside it (`accounts.BusinessStatus`). The declaration is one input,
not the answer.
"""

from dataclasses import dataclass

#: The operative language of the declaration. The English text is a reading aid.
OPERATIVE_LANGUAGE = "de"
LANGUAGES = ("de", "en")


@dataclass(frozen=True)
class Declaration:
    version: str
    texts: dict[str, str]

    def text(self, language: str = OPERATIVE_LANGUAGE) -> str:
        return self.texts[language]


BUSINESS_STATUS_DECLARATIONS: dict[str, Declaration] = {
    "unternehmer-2026-09-18": Declaration(
        version="unternehmer-2026-09-18",
        texts={
            # A draft wording; a correction means a new version here.
            "de": (
                "Ich erkläre, dass ich Unternehmer i.S.d. § 14 BGB bin und diesen Vertrag in "
                "Ausübung meiner selbständigen Tätigkeit abschließe."
            ),
            "en": (
                "I declare that I am an entrepreneur within the meaning of § 14 BGB and that I "
                "enter into this contract in the exercise of my independent professional activity."
            ),
        },
    ),
}

CURRENT_BUSINESS_STATUS_DECLARATION = BUSINESS_STATUS_DECLARATIONS["unternehmer-2026-09-18"]


def declaration_versions() -> tuple[str, ...]:
    """Every version that has ever been published here, current or superseded.

    A stored record may name an old version — it must stay valid, or the record would become
    unreadable the day the wording changes.
    """
    return tuple(BUSINESS_STATUS_DECLARATIONS)


#: The declaration of majority and full legal capacity (issue #123, ADR 0052 amendment
#: 2026-09-19). Recorded exactly like the *Unternehmer* one — verbatim, with its version and the
#: moment — and a new wording is a new version here, never an edit of this one.
#:
#: The account holder must be able to enter into the contract themselves.
#:
#: **No date of birth, and no identity document.** The declaration is the record; a birth date
#: would be one more personal datum with no use we can name (Art. 5(1)(c) GDPR), and a document
#: is refused for the same reason as in `BusinessStatus` (issue #92) and `RightsClaimant`
#: (ADR 0034): identity documents are not stored.
AGE_DECLARATIONS: dict[str, Declaration] = {
    "volljaehrigkeit-2026-09-19": Declaration(
        version="volljaehrigkeit-2026-09-19",
        texts={
            # A draft wording; a change of the wording is a new version here.
            "de": "Ich bestätige, dass ich volljährig und unbeschränkt geschäftsfähig bin.",
            "en": "I confirm that I am of full age and have unrestricted legal capacity.",
        },
    ),
}

CURRENT_AGE_DECLARATION = AGE_DECLARATIONS["volljaehrigkeit-2026-09-19"]


def age_declaration_versions() -> tuple[str, ...]:
    """Every age declaration published here, current or superseded.

    The same rule as `declaration_versions()`: nothing is ever removed from `AGE_DECLARATIONS`, or
    a stored record would stop being readable the day the wording changes.
    """
    return tuple(AGE_DECLARATIONS)
