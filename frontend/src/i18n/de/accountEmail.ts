/**
 * Die Worte der beiden Mail-Aktionen (issue #149, ADR 0054) — „du", wie die ganze App und die
 * Website (Betreiber-Entscheidung 2026-09-19, `src/i18n/address.test.ts`).
 *
 * Die englische Fassung in `en/accountEmail.ts` erklärt, warum die Sätze so vorsichtig sind: eine
 * Anfrage sagt nie, ob es zu einer Adresse ein Konto gibt, und auf dem öffentlichen Host wird das
 * neue Passwort angenommen und kurz danach gesetzt — beides steht hier im Wortlaut.
 */
import type { AccountEmailWords } from "../en/accountEmail";

export const deAccountEmail: AccountEmailWords = {
  accountEmail: {
    account: "Konto",
    backToSignIn: "Zurück zur Anmeldung",
    unavailable: {
      heading: "Dieser Link lässt sich nicht öffnen",
      body: "Fordere auf der Anmeldeseite einen neuen an.",
    },
    forgot: {
      heading: "Neues Passwort setzen",
      lead: "Nenn uns die Adresse, mit der du dich anmeldest, dann schicken wir dir einen Link.",
      label: "E-Mail-Adresse",
      submit: "Link schicken",
      submitting: "Wird geschickt …",
    },
    sent: {
      heading: "Schau in dein Postfach",
      body: "Wenn es zu dieser Adresse ein Konto gibt, ist eine Mail mit einem Link unterwegs. Der Link funktioniert einmal.",
    },
    reset: {
      heading: "Neues Passwort setzen",
      lead: "Wähl ein neues Passwort für dieses Konto. Der Link, den du geöffnet hast, funktioniert nur einmal.",
      label: "Neues Passwort",
      requirement: "Mindestens {{count}} Zeichen, und nichts leicht zu Erratendes.",
      submit: "Passwort speichern",
      submitting: "Wird gespeichert …",
      done: "Dein Passwort steht",
      doneBody: "Du kannst dich jetzt damit anmelden.",
      accepted: "Dein Passwort wird gesetzt",
      acceptedBody: "Melde dich in ein paar Minuten mit der Adresse oben und dem gerade gewählten Passwort an.",
    },
    confirm: {
      heading: "Ist das deine Adresse?",
      lead: "Ein Klick, und wir wissen, dass diese Adresse dich erreicht.",
      submit: "Ja, das ist meine Adresse",
      submitting: "Wird gespeichert …",
      done: "Danke",
      doneBody: "Wir wissen jetzt, dass diese Adresse dich erreicht. Du kannst diese Seite schließen.",
    },
    resend: {
      heading: "Deine E-Mail-Adresse",
      lead: "Wir haben gefragt, ob die Adresse dieses Kontos dich erreicht. Du kannst das Konto so oder so benutzen — es geht nur darum, dass wir dich zuverlässig erreichen.",
      submit: "Mail noch einmal schicken",
      submitting: "Wird geschickt …",
      done: "Die Mail ist unterwegs",
      doneBody: "Öffne den Link darin, dann wissen wir, dass die Adresse dich erreicht.",
      unavailable: "Wir können sie gerade nicht schicken. Schreib uns, dann sieht sich das eine Person an.",
    },
    password: {
      errors: {
        password_too_short: "Das ist zu kurz. Nimm mindestens {{count}} Zeichen.",
        password_too_common: "Dieses Passwort ist zu verbreitet. Wähl bitte ein anderes.",
        password_entirely_numeric: "Nur Zahlen sind zu leicht zu erraten. Nimm Buchstaben oder Wörter dazu.",
        password_too_similar: "Das liegt zu nah an deiner Adresse. Wähl bitte etwas anderes.",
        password_invalid: "Dieses Passwort wurde nicht angenommen. Wähl bitte ein anderes.",
      },
    },
    errors: {
      generic: "Das hat nicht geklappt. Versuch es noch mal.",
      formExpired: "Das Formular ist abgelaufen. Lad die Seite neu und versuch es noch mal.",
      rateLimited: "Zu viele Versuche. Versuch es später noch mal.",
      unavailable: "Das geht gerade nicht. Versuch es in ein paar Minuten noch mal.",
      linkGone: "Dieser Link lässt sich nicht mehr benutzen. Fordere auf der Anmeldeseite einen neuen an.",
    },
  },
};
