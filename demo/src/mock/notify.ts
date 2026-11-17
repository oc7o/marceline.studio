/**
 * Short "this is a demo" lines, said by the mock API and shown by `DemoChrome`. A module-level
 * listener rather than React state, because the mock is installed before React exists.
 */
import { detectLanguage } from "../../../frontend/src/i18n/language";

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
    en: "Added in this tab only — the file never left your device, and a reload resets the demo.",
    de: "Nur in diesem Tab hinzugefügt – die Datei hat dein Gerät nie verlassen, und ein Neuladen setzt die Demo zurück.",
  },
  notInDemo: {
    en: "This is a demo — that step is not available here.",
    de: "Das ist eine Demo – dieser Schritt ist hier nicht verfügbar.",
  },
} as const;

export type DemoMessage = keyof typeof MESSAGES;

type Listener = (text: string) => void;
let listener: Listener | null = null;

export function onDemoMessage(next: Listener): () => void {
  listener = next;
  return () => {
    if (listener === next) listener = null;
  };
}

export function say(message: DemoMessage): void {
  listener?.(MESSAGES[message][detectLanguage()]);
}
