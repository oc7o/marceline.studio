/**
 * Zeigen, dass das Konto dir gehört (ADR 0055) — `/app/verification`.
 *
 * „du", klein, wie die Website (`address.test.ts` prüft das). Die Wortregeln aus
 * `wording.test.ts` gelten auch hier: kein „verifiziert", kein „bestätigt" — ein Zustand sagt, was
 * in unserem Eintrag steht, nicht was rechtlich gilt.
 *
 * Und der Satz, der hier am wichtigsten ist: **wir fragen nie nach einem Ausweis** oder nach einem
 * Geburtsdatum, und wir speichern nichts davon.
 */
import type { VerificationWords } from "../en/verification";

export const deVerification: VerificationWords = {
  verification: {
    heading: "Zeig uns, dass das Konto dir gehört",
    lead: "Du bekommst einen kurzen Code. Setz ihn in die Bio des Kontos, dessen Arbeit wir für dich beobachten, oder schick ihn uns aus diesem Konto — dann öffnet jemand von uns dein öffentliches Profil und notiert, was er gesehen hat.",
    noDocuments:
      "Wir fragen nie nach einem Ausweis oder nach deinem Geburtsdatum, und wir speichern nichts davon.",
    notOnline:
      "Dieser Schritt ist für dein Konto noch nicht eingeschaltet. Wir schreiben dir, sobald es so weit ist.",
    status: {
      heading: "Wie es steht",
      unverified: "Noch hat niemand hingesehen",
      verified: "Unser Team hat es gesehen",
      revoked: "Von unserem Team zurückgenommen",
    },
    method: {
      heading: "Wie es festgehalten wurde",
      bio_code: "der Code stand in der öffentlichen Bio des Kontos",
      dm_code: "der Code kam uns aus dem Konto zu",
      video_call: "ein kurzer Videoanruf mit unserem Team",
      staff_legacy: "von unserem Team außerhalb dieses Schritts notiert",
    },
    claim: {
      heading: "Welches Konto gehört dir?",
      platform: "Plattform",
      platformPlaceholder: "Bitte auswählen",
      handle: "Dein Name dort",
      handleHelp: "Ohne @, genau so geschrieben wie dort.",
      url: "Link zum Profil (freiwillig)",
      urlHelp:
        "Nur eine Adresse auf dieser Plattform selbst. Die Adresse bleibt Text, den ein Mensch von Hand öffnet — nichts von uns ruft sie ab.",
      videoCallOnly:
        "Hier würde ein Code uns nichts sagen: Niemand hat geprüft, wer du bist, bevor du dort posten konntest. Unser Team vereinbart dafür einen kurzen Videoanruf mit dir — auch dann schickst du kein Dokument ein, und wir behalten davon nur das Datum.",
      submit: "Code holen",
      submitting: "Einen Moment …",
      again: "Neuen Code holen",
      pending: "Angekommen. Dein Code steht in Kürze hier.",
    },
    code: {
      heading: "Dein Code",
      place:
        "Kopier ihn in die Bio dieses Kontos oder schick ihn uns von dort in einer Nachricht. Du kannst ihn wieder entfernen, sobald wir hingesehen haben.",
      expires: "Nutzbar bis {{when}}",
      expired: "Dieser Code ist abgelaufen. Hol dir oben einen neuen.",
      placed: "Ich habe ihn eingesetzt",
      placing: "Einen Moment …",
      waiting: "Danke — jemand sieht sich dein Profil in Kürze an.",
      videoCall:
        "Hierfür gibt es keinen Code. Unser Team schreibt dir, um einen kurzen Videoanruf zu vereinbaren.",
    },
    accounts: {
      heading: "Deine Konten",
      empty: "Noch kein Konto genannt.",
      emptyNext: "Nenn oben eines, dann bekommst du dafür einen Code.",
      state: {
        pending: "Wartet darauf, dass du den Code einsetzt",
        placed: "Wartet darauf, dass wir hinsehen",
        verified: "Unser Team hat es gesehen",
        rejected: "Unser Team hat den Code nicht gefunden",
        expired: "Der Code ist abgelaufen",
        revoked: "Von unserem Team zurückgenommen",
      },
      rejectedNext: "Hol dir einen neuen Code und setz ihn noch einmal ein.",
      noLink: "Kein Link angegeben",
    },
    errors: {
      invalid_handle:
        "Das sieht nicht wie ein Name auf dieser Plattform aus. Lass das @ und jeden Link weg.",
      host_not_allowed:
        "Diese Adresse liegt nicht auf der gewählten Plattform. Nimm die Adresse des Profils selbst.",
      url_not_accepted_for_platform:
        "Für diese Auswahl nehmen wir keinen Link. Schreib uns die Adresse stattdessen in deiner Antwort.",
      unknown_platform: "Wähl eine der Plattformen aus der Liste.",
      code_expired: "Dieser Code ist abgelaufen. Hol dir einen neuen.",
      not_open:
        "Dieses Konto ist schon abgeschlossen. Nenn es noch einmal, dann bekommst du einen neuen Code.",
      video_call_required:
        "Das klären wir in einem kurzen Videoanruf, nicht mit einem Code. Unser Team schreibt dir.",
      account_verification_unavailable:
        "Dieser Schritt ist für dein Konto noch nicht eingeschaltet.",
      generic: "Das hat nicht geklappt. Versuch es noch einmal und schreib uns, wenn es bleibt.",
    },
    next: {
      heading: "Was das freischaltet",
      body: "Sobald unser Team es gesehen hat, wartet dein Eintrag an dieser Stelle nicht mehr auf uns, und du kannst für eine Kopie deiner Arbeit eine Meldung vorbereiten.",
      rights: "Deine Rechte und Werke",
    },
  },
};
