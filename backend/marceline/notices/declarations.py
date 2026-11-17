"""The exact texts a customer confirms before a notice draft may be approved, versioned.

Same discipline as `marceline.rights.declarations`: the server owns these words, a new wording is a
new version, and what is stored with a confirmation is a verbatim snapshot of the text shown — never
today's copy. Texts follow docs/legal/answers/01-takedown-notices.md Part One (what the customer
confirms, in substance). The German texts are the originals; English is Marceline's translation.
"""

from dataclasses import dataclass

LANGUAGES = ("en", "de")


@dataclass(frozen=True)
class Declaration:
    version: str
    texts: dict[str, str]

    def text(self, language: str) -> str:
        return self.texts[language]


NOTICE_DECLARATIONS: dict[str, dict[str, Declaration]] = {
    "rights": {
        "notice-rights-2026-09-17": Declaration(
            version="notice-rights-2026-09-17",
            texts={
                "en": (
                    "I confirm that I hold the relevant exclusive right in the identified "
                    "work(s), or am currently authorised to enforce it on behalf of its holder."
                ),
                "de": (
                    "Ich bin Inhaber der maßgeblichen ausschließlichen Rechte an dem/den "
                    "bezeichneten Werk(en) oder befugt, diese durchzusetzen."
                ),
            },
        ),
    },
    "identity": {
        "notice-identity-2026-09-17": Declaration(
            version="notice-identity-2026-09-17",
            texts={
                "en": (
                    "I have reviewed the listed findings and confirm that they concern my "
                    "identified work(s)."
                ),
                "de": (
                    "Ich habe die aufgeführten Fundstellen geprüft und bestätige, dass sie "
                    "mein(e) bezeichnetes/n Werk(e) betreffen."
                ),
            },
        ),
    },
    "authorisation": {
        "notice-authorisation-2026-09-17": Declaration(
            version="notice-authorisation-2026-09-17",
            texts={
                "en": (
                    "I have checked my licences, authorised distributors/affiliates and other "
                    "permissions known to me; I am not aware that the complained-of use is "
                    "authorised."
                ),
                "de": (
                    "Ich habe meine Lizenzen, autorisierten Vertriebspartner/Affiliates und "
                    "sonstigen mir bekannten Erlaubnisse geprüft; mir ist nicht bekannt, dass "
                    "die beanstandete Nutzung erlaubt ist."
                ),
            },
        ),
    },
    "exceptions": {
        "notice-exceptions-2026-09-17": Declaration(
            version="notice-exceptions-2026-09-17",
            texts={
                "en": (
                    "I have considered, for each complained-of use, whether it may be permitted "
                    "under applicable law — for a DMCA notice, including fair use — and "
                    "nevertheless have the required good-faith belief that the use is "
                    "unauthorised."
                ),
                "de": (
                    "Ich habe für jede beanstandete Nutzung geprüft, ob sie nach anwendbarem "
                    "Recht zulässig sein kann - bei einer DMCA-Meldung einschließlich Fair Use "
                    "- und bin gleichwohl in gutem Glauben davon überzeugt, dass die Nutzung "
                    "unerlaubt ist."
                ),
            },
        ),
    },
    "recipient": {
        "notice-recipient-2026-09-17": Declaration(
            version="notice-recipient-2026-09-17",
            texts={
                "en": "I select the listed recipient and notice channel.",
                "de": "Ich wähle den aufgeführten Empfänger und Meldekanal aus.",
            },
        ),
    },
    "accuracy": {
        "notice-accuracy-2026-09-17": Declaration(
            version="notice-accuracy-2026-09-17",
            texts={
                "en": (
                    "I have reviewed the complete notice below and consider its statements "
                    "accurate."
                ),
                "de": (
                    "Ich habe die vollständige Meldung unten geprüft und halte ihre Angaben für "
                    "zutreffend."
                ),
            },
        ),
    },
    "forwarding_warning": {
        "notice-forwarding-2026-09-17": Declaration(
            version="notice-forwarding-2026-09-17",
            texts={
                "en": (
                    "Before sending: this recipient may forward some or all of the notice to the "
                    "website operator, account holder, hosting provider or a transparency/"
                    "research service. The notice contains the identity and contact information "
                    "shown above. I have reviewed those fields before submitting it."
                ),
                "de": (
                    "Vor dem Absenden: Dieser Empfänger kann die Meldung ganz oder teilweise an "
                    "den Website-Betreiber, Kontoinhaber, Hosting-Anbieter oder einen "
                    "Transparenz-/Forschungsdienst weiterleiten. Die oben angezeigten Identitäts- "
                    "und Kontaktdaten sind Bestandteil der Meldung. Ich habe diese Daten vor dem "
                    "Absenden geprüft."
                ),
            },
        ),
    },
}

#: The current version of each confirmation kind — bump the key above and this pointer together;
#: never edit a published version's text in place.
CURRENT: dict[str, Declaration] = {
    kind: max(versions.values(), key=lambda declaration: declaration.version)
    for kind, versions in NOTICE_DECLARATIONS.items()
}

#: Why a customer withdraws or a draft is corrected. Codes; the UI owns the words.
WITHDRAWAL_REASONS = (
    "mistaken_match",
    "rights_changed",
    "already_removed",
    "customer_declined",
    "other",
)
CORRECTION_REASONS = ("wrong_recipient", "wrong_findings", "factual_error", "other")


def current(kind: str) -> Declaration:
    return CURRENT[kind]
