import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { type Schemas, api, errorMessage, unwrap } from "../api/client";
import { queryKeys } from "../api/queries";
import { AvailabilityBadge } from "../components/Availability";
import { MediaGrid } from "../components/MediaGrid";
import { Badge, Button, Dialog, ExternalLink, Flash, StatusBadge } from "../components/ui";
import {
  COMMON_FRAME_KINDS,
  COMMON_FRAME_STATUSES,
  kindLabel,
  wouldDiscount,
} from "../lib/commonFrameKind";
import {
  ALL_GATED_READING,
  allGatedFactLines,
  memberFrameLabel,
  notShowableReason,
  showableMemberFrame,
  summarizeAllGated,
} from "../lib/commonFrameJudging";
import { dateTime } from "../lib/format";
import { type ImageMode, useImageMode } from "../lib/imageMode";

type Group = Schemas["CommonFrameGroupOut"];
type Show = "proposed" | "all";

/**
 * Studio labeling queue for common-frame groups (issue #16, ADR 0015): `build_common_frames`
 * proposes groups of near-identical frames shared by many unrelated videos (intros, logos,
 * watermarks); only a group a human confirms here ever discounts match evidence.
 */
export function CommonFramesScreen() {
  const [show, setShow] = useState<Show>("proposed");
  const [flash, setFlash] = useState<{ message: string; tone: "error" | "info" } | null>(null);
  const [labeling, setLabeling] = useState<Group | null>(null);
  const [mode] = useImageMode();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.commonFrames(show),
    queryFn: () => unwrap(api.GET("/api/v1/review/common-frames", { params: { query: { show } } })),
  });
  const items = query.data?.items ?? [];

  const refresh = (message: string) => {
    setLabeling(null);
    setFlash({ message, tone: "info" });
    void queryClient.invalidateQueries({ queryKey: ["common-frames"] });
    void queryClient.invalidateQueries({ queryKey: queryKeys.counts });
  };

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-display text-display-m">Common-frame groups</h1>
        <label className="text-body-s">
          <span className="sr-only">Show</span>
          <select
            className="rounded-sm border border-border-control bg-surface-100 px-2 py-1.5"
            value={show}
            onChange={(event) => setShow(event.target.value as Show)}
          >
            <option value="proposed">Waiting for a label</option>
            <option value="all">All (including decided)</option>
          </select>
        </label>
        <span className="text-body-s text-ink-muted">
          {items.length} shown{query.data?.truncated ? " (more not shown)" : ""}
        </span>
      </div>
      <p className="rounded-sm border border-caution-ink bg-caution-quiet px-3 py-2 text-body-s text-caution-ink">
        Near-identical frames seen on many unrelated pages: a studio intro, a watermark, a generic
        loading card — or a real coincidence. A group only stops counting as match evidence once you
        confirm it here as something other than “not common”; a proposal alone never discounts
        anything.
      </p>
      <Flash message={flash?.message ?? null} tone={flash?.tone} onClose={() => setFlash(null)} />
      {query.isPending ? (
        <p className="text-ink-subtle">Loading…</p>
      ) : query.isError ? (
        <p className="text-critical-ink">Could not load: {errorMessage(query.error)}</p>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-dashed border-border-control bg-surface-100 p-6 text-center text-ink-muted">
          {show === "proposed" ? (
            <>
              Nothing waiting on a label. The scheduled rebuild only groups active pages, and
              today's only branding-shaped material sits on untargeted ones (issue #16,
              docs/matching/0061-common-frames-review.md) — ask the operator to run{" "}
              <code>manage.py build_common_frames --include-inactive</code> once, then label
              whatever it proposes here before the next scheduled run, or it is removed as stale.
            </>
          ) : (
            "No common-frame groups yet."
          )}
        </div>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((group) => (
            <li key={group.id} className="rounded-sm border border-hairline bg-surface-100 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">Group #{group.id}</span>
                <Badge>{kindLabel(group.kind)}</Badge>
                <StatusBadge status={group.status} />
                {group.discounting ? (
                  <Badge tone="positive">discounting match evidence</Badge>
                ) : null}
                {group.studio_name ? <span className="text-body-s text-ink-muted">{group.studio_name}</span> : null}
              </div>
              <div className="mt-1 text-caption text-ink-muted">
                {group.page_count} page{group.page_count === 1 ? "" : "s"} · {group.site_count} site
                {group.site_count === 1 ? "" : "s"} · {group.member_frame_count} frame
                {group.member_frame_count === 1 ? "" : "s"}
                {group.labeled_at ? ` · labeled ${dateTime(group.labeled_at)}` : ""}
              </div>
              {group.label ? <p className="mt-1 text-caption text-ink-muted">Note: {group.label}</p> : null}
              {allGated(group) ? (
                <AllGatedFacts pages={group.sample_pages} />
              ) : (
                <ul className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5" aria-label="Sample pages">
                  {group.sample_pages.map((sample) => (
                    <li key={sample.page.id} className="rounded-sm border border-hairline p-1.5">
                      <SampleImages sample={sample} mode={mode} />
                      <div className="mt-1 truncate text-caption font-medium" title={sample.page.title}>
                        {sample.page.title || "(no title)"}
                      </div>
                      <div className="flex items-center justify-between gap-1 text-caption text-ink-muted">
                        <span>{sample.page.site}</span>
                        <AvailabilityBadge page={sample.page} />
                      </div>
                      <div className="truncate text-caption">
                        <ExternalLink url={sample.page.url} />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-2 flex flex-wrap gap-2">
                <Button tone="primary" onClick={() => setLabeling(group)}>
                  Label…
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {labeling ? (
        <LabelDialog
          group={labeling}
          onClose={() => setLabeling(null)}
          onDone={refresh}
          onError={(message) => setFlash({ message, tone: "error" })}
        />
      ) : null}
    </section>
  );
}

/** Why a sample shows no picture: the safety hold, the media gate, or nothing stored at all. */
function SampleImages({
  sample,
  mode,
}: {
  sample: Group["sample_pages"][number];
  mode: ImageMode;
}) {
  const marked = showableMemberFrame(sample);
  const label = sample.member_frames.length
    ? sample.member_frames.map((frame) => memberFrameLabel(frame, sample.frame_count)).join("; ")
    : null;
  if (sample.page.safety_status !== "clear") {
    return (
      <>
        <MediaGrid media={[]} hidden mode={mode} size="md" />
        {label ? <p className="mt-1 text-caption text-ink-subtle">{label}</p> : null}
      </>
    );
  }
  if (sample.media.length === 0 && sample.media_gated) {
    return (
      <>
        <p className="text-body-s text-ink-subtle">
          No image: the media gate withholds this page&apos;s images.
        </p>
        {label ? <p className="mt-1 text-caption text-ink-subtle">{label}</p> : null}
      </>
    );
  }
  return (
    <>
      <MediaGrid
        media={sample.media}
        hidden={false}
        mode={mode}
        size="md"
        markedIds={marked ? new Set([marked.media_id]) : undefined}
      />
      {label ? <p className="mt-1 text-caption text-ink-muted">{label}</p> : null}
      {!marked && sample.member_frames.length > 0 ? (
        <p className="mt-1 text-caption text-ink-subtle">
          Cannot show it separately: {notShowableReason(sample.member_frames[0]?.kind ?? "")}.
        </p>
      ) : null}
    </>
  );
}

/** A group nobody can judge by eye: every sample page is behind the gate or a safety hold. */
function allGated(group: Group): boolean {
  return (
    group.sample_pages.length > 0 &&
    group.sample_pages.every((sample) => sample.media.length === 0 && sample.media_gated)
  );
}

/** What is left to judge a group by when no sample page can show an image at all. */
function AllGatedFacts({ pages }: { pages: Group["sample_pages"] }) {
  const facts = summarizeAllGated(pages);
  return (
    <div className="mt-2 rounded-sm border border-caution-ink bg-caution-quiet px-3 py-2 text-caption text-caution-ink">
      <p>
        No sample page of this group may show an image: the media gate (ADR 0018) withholds them.
        Judge it from the stored facts instead, or label it “not common”, which discounts nothing.
      </p>
      <ul className="mt-1.5 list-inside list-disc">
        {allGatedFactLines(facts).map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      <p className="mt-1.5 italic">{ALL_GATED_READING}</p>
    </div>
  );
}

function LabelDialog({
  group,
  onClose,
  onDone,
  onError,
}: {
  group: Group;
  onClose: () => void;
  onDone: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [kind, setKind] = useState(group.kind);
  const [studioName, setStudioName] = useState(group.studio_name);
  const [status, setStatus] = useState(group.status);
  const [label, setLabel] = useState(group.label);
  const [busy, setBusy] = useState(false);

  const submit = () => {
    setBusy(true);
    unwrap(
      api.POST("/api/v1/review/common-frames/{group_id}/label", {
        params: { path: { group_id: group.id } },
        body: { kind, studio_name: studioName, status, label },
      }),
    ).then(
      (result) =>
        onDone(
          `Group #${result.id} labeled ${kindLabel(result.kind).toLowerCase()}, ${result.status}` +
            (result.discounting ? " — now discounting match evidence." : "."),
        ),
      (error: unknown) => {
        setBusy(false);
        onError(errorMessage(error));
      },
    );
  };

  return (
    <Dialog title={`Label common-frame group #${group.id}`} onClose={onClose}>
      <div className="flex flex-col gap-3 text-body-s">
        <label className="block">
          <span className="mb-1 block text-ink-muted">Kind</span>
          <select
            className="w-full rounded-sm border border-hairline-strong px-3 py-1.5"
            value={kind}
            onChange={(event) => setKind(event.target.value as Group["kind"])}
          >
            {COMMON_FRAME_KINDS.map((value) => (
              <option key={value} value={value}>
                {kindLabel(value)}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-ink-muted">Studio name (optional)</span>
          <input
            className="w-full rounded-sm border border-hairline-strong px-3 py-1.5"
            value={studioName}
            onChange={(event) => setStudioName(event.target.value)}
            placeholder="e.g. the studio this intro belongs to"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-ink-muted">Status</span>
          <select
            className="w-full rounded-sm border border-hairline-strong px-3 py-1.5"
            value={status}
            onChange={(event) => setStatus(event.target.value as Group["status"])}
          >
            {COMMON_FRAME_STATUSES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="mb-1 block text-ink-muted">Note (optional, staff-only)</span>
          <textarea
            className="w-full rounded-sm border border-hairline-strong px-3 py-1.5"
            rows={2}
            value={label}
            onChange={(event) => setLabel(event.target.value)}
          />
        </label>
        <p className="text-caption text-ink-muted">
          {wouldDiscount(kind, status)
            ? "This will discount every frame in the group as match evidence."
            : "This label will not discount any evidence (only confirmed, non-“not common” groups do)."}
        </p>
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <Button onClick={onClose}>Cancel</Button>
        <Button tone="primary" onClick={submit} disabled={busy}>
          Save
        </Button>
      </div>
    </Dialog>
  );
}
