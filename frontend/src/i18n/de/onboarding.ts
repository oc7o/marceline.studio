/**
 * Die deutsche Hälfte des `onboarding`-Slice (Issue #115, ADR 0052). Mit der Form des englischen
 * Slice annotiert: ein dort ergänzter Schlüssel, der hier fehlt, ist ein Typfehler.
 *
 * Die Anwendung duzt, wie die öffentliche Website (Operator-Entscheidung 2026-09-19).
 */

import type { OnboardingWords } from "../en/onboarding";

export const deOnboarding: OnboardingWords = {
  onboarding: {
    heading: "Konto einrichten",
    lead: "Vier kurze Schritte. Du kannst die Seite schließen und den Link später wieder öffnen; er merkt sich, wo du warst.",
    progress: "Schritt {{step}} von {{total}}",
    steps: {
      password: "Passwort",
      declaration: "Deine Erklärungen",
      documents: "Die Unterlagen",
      country: "Dein Sitz",
    },
    unavailable: {
      heading: "Dieser Link lässt sich nicht öffnen",
      body: "Schreib der Person, die ihn dir geschickt hat, und bitte um einen neuen.",
    },
    email: {
      label: "Deine Anmeldeadresse",
      fixed: "Diese Adresse gehört zur Einladung und lässt sich hier nicht ändern.",
    },
    password: {
      heading: "Passwort wählen",
      lead: "Du meldest dich mit der Adresse oben und diesem Passwort an.",
      label: "Passwort",
      requirement: "Mindestens {{count}} Zeichen, und nichts leicht zu Erratendes.",
      submit: "Weiter",
      submitting: "Wird gespeichert …",
      errors: {
        password_too_short: "Das ist zu kurz. Mindestens {{count}} Zeichen.",
        password_too_common: "Dieses Passwort ist zu verbreitet. Wähl ein anderes.",
        password_entirely_numeric: "Nur Ziffern sind zu leicht zu erraten. Nimm Buchstaben oder Wörter dazu.",
        password_too_similar: "Das liegt zu nah an deiner Adresse. Wähl etwas anderes.",
        password_invalid: "Dieses Passwort wurde nicht angenommen. Wähl ein anderes.",
      },
    },
    declaration: {
      heading: "Deine Erklärungen",
      lead: "Zwei Sätze, und es sind die, denen du zustimmst. Beide sind auf Deutsch, denn Deutsch ist die Sprache der Vereinbarung.",
      version: "Fassung {{version}}",
      tick: "Ich stimme dieser Erklärung zu.",
      ageHeading: "Dein Alter",
      ageLead: "Du musst volljährig sein. Wir bitten dich, das zu erklären; nach einem Geburtsdatum oder einem Ausweis fragen wir nicht.",
      ageTick: "Ich stimme dieser Erklärung zu.",
      ageRequired: "Bitte stimm der Erklärung zu deinem Alter zu, um fortzufahren.",
      submit: "Weiter",
      submitting: "Wird gespeichert …",
      required: "Bitte stimm der Erklärung zu deiner Tätigkeit zu, um fortzufahren.",
      outdated: "Der Text hat sich geändert, seit diese Seite geöffnet wurde. Lad sie neu und lies die neue Fassung.",
    },
    documents: {
      heading: "Die Unterlagen",
      lead: "Bitte lies beide und stimm ihnen zu. Wir halten fest, welche Fassung dir gezeigt wurde.",
      open: "Lesen (öffnet einen neuen Tab)",
      version: "Fassung {{version}}",
      names: {
        pilot_agreement: "Pilotvereinbarung",
        service_privacy_notice: "Datenschutzinformation zum Dienst",
      },
      tick: "Ich habe beide Unterlagen gelesen und stimme ihnen zu.",
      submit: "Weiter",
      submitting: "Wird gespeichert …",
      allRequired: "Bitte stimm beiden Unterlagen zu, um fortzufahren.",
      notPublished: "Die Unterlagen sind noch nicht so weit, deshalb lässt sich dieser Schritt heute nicht abschließen. Nichts von dem, was du eingegeben hast, geht verloren — öffne den Link in ein paar Tagen erneut oder schreib der Person, die dich eingeladen hat.",
    },
    country: {
      heading: "Wo hat deine Tätigkeit ihren Sitz?",
      lead: "Der Pilotbetrieb steht nur in der Europäischen Union offen.",
      label: "Land",
      choose: "Bitte wählen",
      submit: "Abschließen",
      submitting: "Wird abgeschlossen …",
      required: "Bitte wähle ein Land.",
      outside: {
        heading: "Bei dir noch nicht offen",
        body: "Der Pilotbetrieb läuft nur in der Europäischen Union, deshalb können wir dir heute kein Konto einrichten. Es wurde nichts angelegt, und du kannst uns schreiben, wenn das ein Versehen ist.",
      },
    },
    done: {
      heading: "Dein Konto steht",
      body: "Du bist angemeldet. Leg als Nächstes dein erstes Werk an — die Datei bleibt auf deinem Rechner.",
      cta: "Erstes Werk anlegen",
      accountStep:
        "Danach folgt noch ein kurzer Schritt: Du zeigst uns, dass das Konto, dessen Arbeit wir beobachten, dir gehört. Ein Code, eine Minute, und kein Dokument irgendeiner Art.",
      accountStepCta: "Zeig uns, dass das Konto dir gehört",
    },
    pending: {
      heading: "Dein Konto wird eingerichtet",
      body: "Wir haben alles, was wir brauchen. Dein Konto wird jetzt angelegt; melde dich in ein paar Minuten mit der Adresse oben und dem gerade gewählten Passwort an.",
      cta: "Zur Anmeldung",
    },
    checking: {
      heading: "Wir sehen uns dein Konto an",
      body: "Eine Person von uns prüft deine Angaben, bevor alles freigeschaltet ist. Du kannst deine Werke schon jetzt anlegen; wenn etwas fehlt, schreiben wir dir.",
    },
    errors: {
      generic: "Das hat nicht geklappt. Versuch es noch mal.",
      formExpired: "Das Formular ist abgelaufen. Lad die Seite neu und versuch es noch mal.",
      rateLimited: "Zu viele Versuche. Versuch es später noch mal.",
      businessStatus: "Dein Konto wird von Hand eingerichtet. Es geht nichts verloren — wir schreiben dir, sobald es so weit ist.",
      unavailable: "Wir können das gerade nicht abschließen. Es geht nichts verloren — öffne den Link in ein paar Minuten noch einmal.",
    },
  },
};
