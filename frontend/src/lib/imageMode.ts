import { useCallback, useSyncExternalStore } from "react";

/**
 * Image mode, remembered per browser.
 *
 * - "privacy" (default): no image is requested at all.
 * - "load": images auto-load, always through our own passthrough endpoint.
 *
 * Anything unreadable (no storage, blocked storage, unknown value) means privacy.
 */
export type ImageMode = "privacy" | "load";

export const IMAGE_MODE_KEY = "marceline.imageMode";
export const DEFAULT_IMAGE_MODE: ImageMode = "privacy";

type ReadableStorage = Pick<Storage, "getItem">;
type WritableStorage = Pick<Storage, "setItem">;

export function readImageMode(storage: ReadableStorage | null | undefined): ImageMode {
  try {
    return storage?.getItem(IMAGE_MODE_KEY) === "load" ? "load" : DEFAULT_IMAGE_MODE;
  } catch {
    return DEFAULT_IMAGE_MODE;
  }
}

export function writeImageMode(storage: WritableStorage | null | undefined, mode: ImageMode): void {
  try {
    storage?.setItem(IMAGE_MODE_KEY, mode);
  } catch {
    // Storage blocked: the mode lasts for this page only.
  }
}

function browserStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

const listeners = new Set<() => void>();
let memoryMode: ImageMode | null = null;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === IMAGE_MODE_KEY) {
      memoryMode = null;
      listener();
    }
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

function snapshot(): ImageMode {
  return memoryMode ?? readImageMode(browserStorage());
}

export function setImageMode(mode: ImageMode): void {
  memoryMode = mode;
  writeImageMode(browserStorage(), mode);
  listeners.forEach((listener) => listener());
}

/** Test helper: forget the in-memory mode so the next read comes from storage again. */
export function resetImageModeCache(): void {
  memoryMode = null;
}

export function useImageMode(): [ImageMode, (mode: ImageMode) => void] {
  const mode = useSyncExternalStore(subscribe, snapshot, () => DEFAULT_IMAGE_MODE);
  const set = useCallback((next: ImageMode) => setImageMode(next), []);
  return [mode, set];
}
