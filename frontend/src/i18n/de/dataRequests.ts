/**
 * `/app/data` auf Deutsch (ADR 0056, issue #133). „du", klein, wie im Rest der App.
 *
 * Die Regeln stehen im englischen Slice: eine Anfrage ist eine Anfrage, nie eine Antwort, und die
 * Löschung liest sich wie das, was sie ist. Der Monat ist der gesetzliche (Art. 12 Abs. 3 DSGVO) —
 * `wording.test.ts` verbietet zugesagte Dauern in Stunden oder Tagen, und das ist hier keine.
 */
import type { DataRequestWords } from "../en/dataRequests";

export const deDataRequests: DataRequestWords = {
  dataRequests: {
    navLink: "Meine Daten",
    heading: "Meine Daten",
    lead: "Frag eine Kopie von allem an, was in deinem Konto liegt — oder dass wir es löschen und dein Pilotprojekt beenden. Beides beantwortet ein Mensch von Hand.",
    notOnline:
      "Die Anfrage von hier aus ist noch nicht freigeschaltet. Schreib uns stattdessen, wir machen dasselbe von Hand.",
    deadline: "Wir antworten innerhalb eines Monats (Art. 12 Abs. 3 DSGVO).",
    contact:
      "Du kannst auch einfach an {{email}} schreiben — kein Formular, kein Nachweis über dein Konto hinaus.",
    export: {
      heading: "Schick mir eine Kopie meiner Daten",
      body: "Alles, was in deinem Konto liegt: deine Originale und ihre gespeicherten Fingerabdrücke, die gefundenen übereinstimmenden Kopien, deine Rechteangaben, deine Meldungsentwürfe und die Rechnungen. Eine maschinenlesbare Datei (Art. 15, Art. 20 DSGVO).",
      submit: "Kopie anfragen",
      submitting: "Wird gesendet…",
      open: "Angefragt am {{date}}.",
      done: "Beantwortet am {{date}}.",
      empty: "Du hast noch keine Kopie angefragt.",
    },
    deletion: {
      heading: "Meine Daten löschen und das Pilotprojekt beenden",
      body: "Ein Mensch setzt das Enddatum, und alles aus deinem Konto wird 30 Tage danach gelöscht — das Fenster ist dafür da, dass du deine Kopie vorher noch mitnehmen kannst.",
      kept: "Zwei Dinge bleiben: Rechnungen, die wir steuerrechtlich aufbewahren müssen, und was unter einer rechtlichen Sperre steht. Unser eigener Seitenindex gehört nicht zu deinen Daten und ist hier nicht gemeint.",
      phrase:
        "Tippe {{phrase}} ein, um die Anfrage zu senden. Das lässt sich nicht rückgängig machen, sobald wir sie umgesetzt haben.",
      phraseLabel: "Das Wort von oben",
      submit: "Löschung anfragen",
      submitting: "Wird gesendet…",
      open: "Angefragt am {{date}}. Du kannst sie zurückziehen, bis wir sie umsetzen.",
      done: "Beantwortet am {{date}}.",
      empty: "Du hast keine Löschung angefragt.",
    },
    status: {
      heading: "Stand",
      requested: "Eingegangen",
      in_progress: "In Arbeit",
      done: "Beantwortet",
      withdrawn: "Zurückgezogen",
    },
    due: "Antwort spätestens am {{date}}.",
    withdraw: "Anfrage zurückziehen",
    withdrawing: "Wird zurückgezogen…",
    history: {
      heading: "Frühere Anfragen",
      requestedOn: "Angefragt am {{date}}",
      settledOn: "Abgeschlossen am {{date}}",
    },
    pending: "Angekommen. Erscheint gleich hier.",
    errors: {
      already_open: "Diese Anfrage ist schon offen — wir haben sie.",
      not_open: "Es gibt keine offene Anfrage dieser Art mehr.",
      confirmation_mismatch: "Das Wort stimmt nicht. Tippe es genau so ein, wie es oben steht.",
      unknown_kind: "Das kannst du hier nicht anfragen.",
      data_request_write_unavailable:
        "Anfragen können gerade nicht angenommen werden. Schreib uns, wir tragen sie ein.",
      processing_paused: "Anfragen können gerade nicht angenommen werden. Bitte später erneut.",
      too_many_requests: "Das waren viele Anfragen auf einmal. Bitte später erneut.",
      generic: "Das hat nicht geklappt. Bitte erneut versuchen oder uns schreiben.",
    },
  },
};
