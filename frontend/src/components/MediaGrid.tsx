import { useEffect, useRef, useState } from "react";

import { type MediaRef, mediaImageUrl } from "../api/client";
import type { ImageMode } from "../lib/imageMode";

/**
 * Page images. Every rule about showing a copy of the material lives in this one component.
 *
 * **Privacy** (frontend/CLAUDE.md, hard):
 * - `hidden` (page in safety review or blocked): never an `<img>`, in any mode.
 * - privacy mode, the default: placeholders, **no request at all**.
 * - load mode: an `<img>` from our passthrough only (`mediaImageUrl`), never an upstream URL.
 * - every image lazy-loads on a box the parent sizes, so a queue of a hundred thumbnails fetches
 *   only what the reviewer scrolls to. That is a privacy rule as much as a performance one: each
 *   image is a real request, and one the media's host can see us make.
 *
 * **Media handling** (the design system's own rule, and this is the surface it was written for):
 * unreviewed media sits behind `blur-thumb` on a `surface-inset` frame with **one** reveal control
 * for the group, and re-blurs when the group loses focus or the reviewer leaves the page. Load mode
 * decides whether the bytes are fetched; the blur decides whether they are *looked at*, and the two
 * are deliberately separate — a reviewer who wants a thumbnail loaded for one row has not thereby
 * asked for a wall of unblurred frames.
 */
export function MediaGrid({
  media,
  hidden,
  mode,
  size = "md",
  markedIds,
  markedLabel = "repeated frame",
}: {
  media: MediaRef[];
  hidden: boolean;
  mode: ImageMode;
  size?: "sm" | "md";
  /** Ids in `media` to call out visually (a token border/badge, never a literal colour): the
   * common-frame screen marks the one frame that repeats so the eye can compare it to the rest. */
  markedIds?: ReadonlySet<number>;
  markedLabel?: string;
}) {
  const [revealed, setRevealed] = useState(false);
  const group = useRef<HTMLDivElement>(null);

  // Leaving the page re-blurs, exactly as losing focus does: the next glance starts covered.
  useEffect(() => {
    if (!revealed) return;
    const reblur = () => setRevealed(false);
    document.addEventListener("visibilitychange", reblur);
    return () => document.removeEventListener("visibilitychange", reblur);
  }, [revealed]);

  if (hidden) {
    return (
      <p className="rounded-sm border border-caution-ink bg-caution-quiet px-3 py-2 text-body-s text-caution-ink">
        Media hidden: this page is flagged for safety review.
      </p>
    );
  }
  if (media.length === 0) {
    return <p className="text-body-s text-ink-subtle">No page images.</p>;
  }
  // Evidence frames stay square (`rounded-none`); the frame itself is the inset surface.
  const tile = size === "sm" ? "h-14 w-24" : "aspect-video w-full";
  return (
    <div
      ref={group}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setRevealed(false);
      }}
    >
      {mode === "load" ? (
        <button
          type="button"
          onClick={() => setRevealed((value) => !value)}
          aria-pressed={revealed}
          className="mb-1 rounded-sm border border-hairline-strong bg-surface-100 px-2 py-0.5 text-caption text-ink-muted hover:bg-surface-300"
        >
          {revealed ? "Blur images" : "Reveal images"}
        </button>
      ) : null}
      <ul
        className={size === "sm" ? "flex gap-1" : "grid grid-cols-3 gap-2 xl:grid-cols-4"}
        aria-label="Page images"
      >
        {[...media]
          .sort(
            (a, b) =>
              Number(markedIds?.has(b.id) ?? false) - Number(markedIds?.has(a.id) ?? false),
          )
          .map((item) => {
            const marked = markedIds?.has(item.id) ?? false;
            return (
              <li
                key={item.id}
                className={`relative ${tile} overflow-hidden rounded-none border bg-surface-inset ${
                  marked ? "border-accent ring-2 ring-accent" : "border-hairline"
                }`}
              >
                {marked ? (
                  <span className="absolute left-0 top-0 z-10 rounded-none bg-accent-quiet px-1 text-caption text-accent">
                    {markedLabel}
                  </span>
                ) : null}
                {mode === "load" ? (
                  <PassthroughImage media={item} revealed={revealed} />
                ) : (
                  <span className="flex h-full items-center justify-center text-caption text-ink-subtle">
                    privacy mode
                  </span>
                )}
              </li>
            );
          })}
      </ul>
    </div>
  );
}

function PassthroughImage({ media, revealed }: { media: MediaRef; revealed: boolean }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <span className="flex h-full items-center justify-center text-caption text-ink-subtle">
        image unavailable
      </span>
    );
  }
  return (
    <img
      src={mediaImageUrl(media.id)}
      alt={`${media.kind} ${media.id}`}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
      className={`h-full w-full object-cover transition-[filter] duration-120 ease-standard ${
        revealed ? "" : "blur-thumb"
      }`}
      onError={() => setFailed(true)}
    />
  );
}
