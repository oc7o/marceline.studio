import { useId, useState } from "react";

import { LEGEND_NOTES, STATUS_LEGEND } from "../lib/provenance";
import { Button, StatusBadge } from "./ui";

/** Help popover for the Clusters screen: what proposed / confirmed / rejected mean. */
export function StatusLegend() {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  return (
    <div className="relative">
      <Button onClick={() => setOpen((value) => !value)} aria-expanded={open} aria-controls={panelId}>
        What do the statuses mean?
      </Button>
      {open ? (
        <div
          id={panelId}
          role="region"
          aria-label="Status legend"
          className="absolute left-0 z-30 mt-2 w-[28rem] max-w-[90vw] rounded-lg border border-hairline bg-surface-100 p-4 text-body-s shadow-lg"
        >
          <dl className="space-y-2">
            {STATUS_LEGEND.map((entry) => (
              <div key={entry.status}>
                <dt>
                  <StatusBadge status={entry.status} />
                </dt>
                <dd className="mt-1 text-ink-muted">{entry.text}</dd>
              </div>
            ))}
          </dl>
          <ul className="mt-3 list-disc space-y-1 pl-5 text-ink-muted">
            {LEGEND_NOTES.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
          <div className="mt-3 flex justify-end">
            <Button onClick={() => setOpen(false)}>Close</Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
