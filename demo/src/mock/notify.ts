/**
 * Short "this is a demo" lines, said by the mock API and shown by `DemoChrome`, in the
 * language the app is showing.
 */
import { currentLanguage } from "../runtime/language";

const MESSAGES = {
  saved: {
    en: "Saved in this tab only — a reload resets the demo.",
    de: "Nur in diesem Tab gespeichert – ein Neuladen setzt die Demo zurück.",
  },
  signedOut: {
    en: "Signed out of the demo. Any e-mail address and password signs you back in.",
    de: "Aus der Demo abgemeldet. Jede E-Mail-Adresse mit beliebigem Passwort meldet dich wieder an.",
  },
  signedIn: {
    en: "Signed in to the demo — nothing you typed was sent anywhere.",
    de: "In der Demo angemeldet – nichts von deiner Eingabe wurde irgendwohin gesendet.",
  },
  sampleLink: {
    en: "A sample address — this page does not exist.",
    de: "Eine Beispieladresse – diese Seite gibt es nicht.",
  },
  download: {
    en: "A sample file, generated in your browser.",
    de: "Eine Beispieldatei, in deinem Browser erzeugt.",
  },
  added: {
    en: "Added in this tab only — the file never left your device.",
    de: "Nur in diesem Tab hinzugefügt – die Datei hat dein Gerät nie verlassen.",
  },
  noMail: {
    en: "The demo sends no mail — nothing left this page.",
    de: "Die Demo verschickt keine E-Mails – nichts hat diese Seite verlassen.",
  },
  passwordKept: {
    en: "Nothing was stored — any e-mail address and password signs you in to the demo.",
    de: "Nichts wurde gespeichert – jede E-Mail-Adresse mit beliebigem Passwort meldet dich in der Demo an.",
  },
  notInDemo: {
    en: "This is a demo — that step is not available here.",
    de: "Das ist eine Demo – dieser Schritt ist hier nicht verfügbar.",
  },
} as const;

export type DemoMessage = keyof typeof MESSAGES;

type Listener = (text: string) => void;
let listener: Listener | null = null;
/** Said before the toast was on the page (a download opened by its own address): shown once it is. */
let waiting: string | null = null;

export function onDemoMessage(next: Listener): () => void {
  listener = next;
  if (waiting !== null) {
    next(waiting);
    waiting = null;
  }
  return () => {
    if (listener === next) listener = null;
  };
}

export function say(message: DemoMessage): void {
  const text = MESSAGES[message][currentLanguage()];
  if (listener) listener(text);
  else waiting = text;
}
