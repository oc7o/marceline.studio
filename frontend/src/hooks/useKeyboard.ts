import { useEffect, useLayoutEffect, useRef } from "react";

import { type Command, keyToCommand } from "../lib/keyboard";

/** Global review shortcuts; keys typed into inputs are ignored (see `keyToCommand`). */
export function useKeyboard(handler: (command: Command) => void): void {
  const latest = useRef(handler);
  useLayoutEffect(() => {
    latest.current = handler;
  });
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.repeat && event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const command = keyToCommand(event);
      if (!command) return;
      event.preventDefault();
      latest.current(command);
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);
}
