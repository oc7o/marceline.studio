"""The exact texts a customer confirms, versioned (ADR 0034).

The server owns these words, not the frontend: what is stored with a declaration must be exactly
what was shown, so the API sends the text and the screen renders it verbatim. A new wording is a
new version — never edit a published text in place, because stored declarations name their version
and carry a snapshot of the text they confirmed.

Factual wording only: a self-declaration is the customer's statement, not a verification, and it
enables no notice draft by itself.

**Every text here is the customer speaking in the first person about their own affairs.** That is
why a declaration may say what a customer's own mandate covers while no string of ours ever may
(`frontend/src/i18n/wording.test.ts`): the proposition belongs to the person making it. A text that
characterises the customer's documents *for* them does not belong in this file — or anywhere.
"""

from dataclasses import dataclass

LANGUAGES = ("en", "de")


@dataclass(frozen=True)
class Declaration:
    version: str
    texts: dict[str, str]

    def text(self, language: str) -> str:
        return self.texts[language]


WORK_DECLARATIONS: dict[str, Declaration] = {
    "work-2026-09-17": Declaration(
        version="work-2026-09-17",
        texts={
            "en": (
                "I declare that the organisation I act for holds the right stated above in this "
                "work, or is currently authorised by the rights holder named above to act in "
                "relation to that right. To my knowledge, this right or authority has not been "
                "revoked or transferred. I will update this record promptly if that changes. I "
                "understand that this declaration is recorded as a self-declaration: it is not "
                "proof of the rights and does not by itself make notice drafts available."
            ),
            "de": (
                "Ich erkläre, dass die Organisation, für die ich handle, das oben angegebene "
                "Recht an diesem Werk innehat oder derzeit von der oben genannten "
                "Rechteinhaberin bzw. dem oben genannten Rechteinhaber ermächtigt ist, in Bezug "
                "auf dieses Recht zu handeln. Nach meiner Kenntnis wurde dieses Recht bzw. diese "
                "Ermächtigung weder widerrufen noch übertragen. Ändert sich das, aktualisiere ich "
                "diesen Eintrag unverzüglich. Mir ist bewusst, dass diese Erklärung als "
                "Selbstauskunft gespeichert wird: Sie ist kein Nachweis der Rechte und ermöglicht "
                "für sich allein keine Meldungsentwürfe."
            ),
        },
    ),
}

PARTNER_DECLARATIONS: dict[str, Declaration] = {
    "partners-none-2026-09-17": Declaration(
        version="partners-none-2026-09-17",
        texts={
            "en": (
                "I have reviewed the licences and distribution agreements of the organisation I "
                "act for. It has no licensed partners, authorised distributors, affiliates or "
                "platforms that are permitted to publish its works. I will add any such partner "
                "here as soon as one exists."
            ),
            "de": (
                "Ich habe die Lizenz- und Vertriebsvereinbarungen der Organisation, für die ich "
                "handle, geprüft. Sie hat keine Lizenzpartner, autorisierten Vertriebspartner, "
                "Affiliates oder Plattformen, die ihre Werke veröffentlichen dürfen. Sobald es "
                "einen solchen Partner gibt, trage ich ihn hier ein."
            ),
        },
    ),
}

#: The customer's second statement about one work (ADR 0034 amendment 2026-09-18): that the
#: documents we hold — the ones staff recorded as formally complete — give the organisation the
#: right or authority it declared. This is the affirmative proposition we never make for a
#: customer, so the customer makes it, in their own words, and we store it.
AUTHORITY_DECLARATIONS: dict[str, Declaration] = {
    "authority-2026-09-18": Declaration(
        version="authority-2026-09-18",
        texts={
            "en": (
                "I have read the documents the organisation I act for has given Marceline for "
                "this work, and I state that they give it the right or the authority I declared "
                "here. Where I act as an authorised agent, the mandate I rely on covers preparing "
                "and submitting notices about this work. Nobody at Marceline has assessed for me "
                "what these documents mean in law; where I needed that, I took my own advice. I "
                "will withdraw this declaration promptly if it stops being true."
            ),
            "de": (
                "Ich habe die Unterlagen gelesen, die die Organisation, für die ich handle, "
                "Marceline zu diesem Werk übergeben hat, und erkläre, dass sie ihr das Recht bzw. "
                "die Befugnis geben, die ich hier angegeben habe. Soweit ich als Bevollmächtigte "
                "handle, umfasst die Vollmacht, auf die ich mich stütze, das Vorbereiten und "
                "Einreichen von Meldungen zu diesem Werk. Niemand bei Marceline hat für mich "
                "geprüft, was diese Unterlagen rechtlich bedeuten; wo ich das brauchte, habe ich "
                "mir selbst Rat eingeholt. Ich ziehe diese Erklärung unverzüglich zurück, wenn sie "
                "nicht mehr zutrifft."
            ),
        },
    ),
}

AUTHORITY_WITHDRAWALS: dict[str, Declaration] = {
    "authority-withdraw-2026-09-18": Declaration(
        version="authority-withdraw-2026-09-18",
        texts={
            "en": (
                "I withdraw my declaration about the right or authority our documents give for "
                "this work. Until the organisation I act for declares it again, it makes no such "
                "statement in Marceline. The documents themselves stay on file."
            ),
            "de": (
                "Ich ziehe meine Erklärung dazu zurück, welches Recht bzw. welche Befugnis unsere "
                "Unterlagen für dieses Werk geben. Bis die Organisation, für die ich handle, sie "
                "erneut erklärt, macht sie dazu in Marceline keine Angabe. Die Unterlagen selbst "
                "bleiben gespeichert."
            ),
        },
    ),
}

WITHDRAWALS: dict[str, Declaration] = {
    "withdraw-2026-09-17": Declaration(
        version="withdraw-2026-09-17",
        texts={
            "en": (
                "I withdraw the self-declaration for this work. From now on the organisation I act "
                "for makes no statement about its rights to this work in Marceline, until it "
                "declares them again."
            ),
            "de": (
                "Ich ziehe die Selbstauskunft zu diesem Werk zurück. Die Organisation, für die ich "
                "handle, macht in Marceline ab sofort keine Angabe mehr zu ihren Rechten an diesem "
                "Werk, bis sie diese erneut erklärt."
            ),
        },
    ),
}

#: Why a customer withdraws a self-declaration. Codes; the UI owns the words.
WITHDRAWAL_REASONS = ("rights_transferred", "authority_ended", "declared_in_error", "other")

CURRENT_WORK_DECLARATION = WORK_DECLARATIONS["work-2026-09-17"]
CURRENT_AUTHORITY_DECLARATION = AUTHORITY_DECLARATIONS["authority-2026-09-18"]
CURRENT_AUTHORITY_WITHDRAWAL = AUTHORITY_WITHDRAWALS["authority-withdraw-2026-09-18"]
CURRENT_WITHDRAWAL = WITHDRAWALS["withdraw-2026-09-17"]
CURRENT_PARTNER_DECLARATION = PARTNER_DECLARATIONS["partners-none-2026-09-17"]
