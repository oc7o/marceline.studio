import { useState } from "react";

import { type AvailabilityTone, availabilityInfo } from "../lib/availability";

/* A page that is still up is the live problem, so `online` is the red one here, not the green. */
const TONES: Record<AvailabilityTone, string> = {
  online: "bg-critical-quiet text-critical-ink",
  removed: "bg-positive-quiet text-positive-ink",
  unknown: "bg-surface-300 text-ink",
};

/** online / removed / unknown, plus when the crawler last checked ("checked 3 h ago" / "never checked"). */
export function AvailabilityBadge({
  page,
  now,
}: {
  page: { availability: string; availability_checked_at: string | null };
  now?: Date;
}) {
  const [mounted] = useState(() => new Date());
  const info = availabilityInfo(page, now ?? mounted);
  return (
    <span className="inline-flex items-center gap-1.5 text-caption">
      <span className={`rounded-full px-1.5 py-0.5 text-overline uppercase ${TONES[info.tone]}`}>
        {info.label}
      </span>
      <span className="text-ink-subtle" title={page.availability_checked_at ?? undefined}>
        {info.checked}
      </span>
    </span>
  );
}
