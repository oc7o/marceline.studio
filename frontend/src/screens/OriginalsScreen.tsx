import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useRef, useState } from "react";

import { type Schemas, api, errorMessage, unwrap } from "../api/client";
import { Button, Flash, StatusBadge } from "../components/ui";
// Types only: the client itself (worker, PDQ WASM) is loaded when a run starts, never with the app.
import type { FingerprintProgress } from "../lib/fingerprintClient";
import { overallProgress } from "../lib/fingerprintProgress";
import { clock, creatorLabel } from "../lib/format";
import { addReferenceCommand, count } from "../lib/overviewFormat";
import { OWNER_UNASSIGNED, OWNER_UNCHOSEN, ownerChoice } from "../lib/ownerChoice";
import { BROWSER_PIPELINE, DEFAULT_FPS, asRequestBody } from "../lib/videoFingerprint";
import { STATUS_FILTERS, type StatusFilter } from "../router";

type Original = Schemas["OriginalOut"];
type OriginalPair = Schemas["OriginalPairOut"];
type OriginalM2 = Schemas["OriginalM2Out"];

function statusFilter(status: string): StatusFilter {
  return STATUS_FILTERS.find((value) => value === status) ?? "all";
}

export function OriginalsScreen() {
  const query = useQuery({
    queryKey: ["originals"],
    queryFn: () => unwrap(api.GET("/api/v1/review/originals")),
  });
  return (
    <div className="flex flex-col gap-4">
      <h1 className="font-display text-display-m">Originals</h1>
      {query.isPending ? (
        <p className="text-ink-subtle">Loading…</p>
      ) : query.isError ? (
        <p className="text-critical-ink">Could not load: {errorMessage(query.error)}</p>
      ) : query.data.length === 0 ? (
        <div className="rounded-md border border-dashed border-border-control bg-surface-100 p-10 text-center text-ink-muted">
          No originals imported yet.
        </div>
      ) : (
        <div className="overflow-x-auto rounded-md border border-hairline bg-surface-100">
          <table className="w-full text-body-s">
            <thead className="text-left text-caption text-ink-subtle">
              <tr className="border-b border-hairline">
                <th className="px-3 py-2 font-medium">Original</th>
                <th className="px-3 py-2 font-medium">Duration</th>
                <th className="px-3 py-2 font-medium">Frames (shared)</th>
                <th className="px-3 py-2 font-medium">Matches strong · weak</th>
                <th className="px-3 py-2 font-medium">Matches proposed · confirmed · rejected</th>
                <th className="px-3 py-2 font-medium">Leaks confirmed · rejected · proposed</th>
                <th className="px-3 py-2 font-medium">Clusters</th>
              </tr>
            </thead>
            <tbody>
              {query.data.map((original) => (
                <OriginalRow key={original.id} original={original} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <AddOriginalPanel />
    </div>
  );
}

type FileStatus = "waiting" | "hashing" | "uploading" | "done" | "failed";

interface FileState {
  id: number;
  name: string;
  size: number;
  status: FileStatus;
  /** 0..1 within the current stage. */
  progress: number;
  stage: string;
  message: string;
  referenceId: number | null;
}

const STAGE_LABEL: Record<string, string> = {
  digest: "reading the file (SHA-256)",
  hash: "reading the file and hashing frames",
  done: "hashed",
};

/**
 * Register an original from the browser.
 *
 * The file is read locally and never uploaded: only the PDQ hashes and the metadata below leave
 * this machine. The CLI stays available for large batches and for files the browser cannot decode.
 */
function AddOriginalPanel() {
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<string[]>([]);
  const [owner, setOwner] = useState<string>(OWNER_UNCHOSEN);
  const [files, setFiles] = useState<FileState[]>([]);
  const [flash, setFlash] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const cancel = useRef({ aborted: false });

  const creators = useQuery({
    queryKey: ["originals", "creators"],
    queryFn: () => unwrap(api.GET("/api/v1/originals/creators")),
  });

  const organisations = useQuery({
    queryKey: ["originals", "organisations"],
    queryFn: () => unwrap(api.GET("/api/v1/originals/organisations")),
  });
  const organisationId = ownerChoice(owner);

  const countFailure = useMutation({
    mutationFn: (body: Schemas["ClientEventIn"]) =>
      unwrap(api.POST("/api/v1/originals/client-events", { body })),
  });

  const toggle = (name: string) => {
    setPicked((current) =>
      current.includes(name) ? current.filter((value) => value !== name) : [...current, name],
    );
  };

  const update = (id: number, patch: Partial<FileState>) => {
    setFiles((current) => current.map((file) => (file.id === id ? { ...file, ...patch } : file)));
  };

  const start = async () => {
    const chosen = [...(input.current?.files ?? [])];
    if (chosen.length === 0 || picked.length === 0 || organisationId === undefined) return;
    cancel.current = { aborted: false };
    setFlash(null);
    setRunning(true);
    const initial = chosen.map((file, index) => ({
      id: index,
      name: file.name,
      size: file.size,
      status: "waiting" as FileStatus,
      progress: 0,
      stage: "",
      message: "",
      referenceId: null,
    }));
    setFiles(initial);
    let client: typeof import("../lib/fingerprintClient");
    try {
      client = await import("../lib/fingerprintClient");
    } catch (error: unknown) {
      setFlash(`Could not load the fingerprinting code: ${errorMessage(error)}`);
      setRunning(false);
      return;
    }
    for (const [index, file] of chosen.entries()) {
      if (cancel.current.aborted) {
        update(index, { status: "failed", message: "Cancelled" });
        continue;
      }
      try {
        update(index, { status: "hashing", stage: STAGE_LABEL.digest ?? "" });
        const fingerprint = await client.fingerprintFile(file, {
          creators: picked,
          signal: cancel.current,
          onProgress: (progress: FingerprintProgress) =>
            update(index, {
              // Digest and hashing run at once; the label names the slower half.
              stage: (progress.framesTotal > 0 ? STAGE_LABEL.hash : STAGE_LABEL.digest) ?? "",
              progress: overallProgress(progress),
            }),
        });
        update(index, { status: "uploading", stage: "sending hashes", progress: 1 });
        const result = await unwrap(
          api.POST("/api/v1/originals", {
            body: {
              organisation_id: organisationId,
              fingerprint: asRequestBody(fingerprint),
              creators: picked,
              source_note: "Registered in the browser client",
            },
          }),
        );
        update(index, {
          status: "done",
          referenceId: result.reference_id,
          message: `${result.frames} positions · ${result.hashes} hashes · ${result.pipeline}`,
        });
      } catch (error: unknown) {
        const message = errorMessage(error);
        update(index, { status: "failed", message });
        // The counters that decide whether cloud fingerprinting is worth building (#59).
        const kind = client.failureKind(error);
        if (kind !== null) {
          countFailure.mutate({
            kind,
            detail: message.slice(0, 200),
            container: file.type,
            codec: "",
          });
        }
      }
    }
    setRunning(false);
    void queryClient.invalidateQueries({ queryKey: ["originals"] });
  };

  return (
    <section className="rounded-md border border-hairline bg-surface-100 p-4 text-body-s">
      <h2 className="mb-1 font-semibold">Add an original</h2>
      <p className="mb-3 text-ink-muted">
        The video is read <strong>on this machine only</strong>: the browser decodes it, hashes the frames and uploads
        nothing but the PDQ fingerprints and the metadata (title, duration, size, SHA-256, creators). No file, no frame,
        no thumbnail ever leaves this computer. Hashes are tagged{" "}
        <code className="font-mono">{BROWSER_PIPELINE}</code> because a browser decodes differently than the server
        (ADR 0019), and registering an original is a <strong>claim</strong>, not proof of ownership — a human still
        reviews every match.
      </p>

      <fieldset className="mb-3">
        <legend className="mb-1 font-medium">Creators (click in order — the first is the rights holder)</legend>
        {creators.isPending ? (
          <p className="text-ink-subtle">Loading creators…</p>
        ) : creators.isError ? (
          <p className="text-critical-ink">Could not load creators: {errorMessage(creators.error)}</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {creators.data.map((creator) => {
              const position = picked.indexOf(creator.name);
              return (
                <button
                  key={creator.id}
                  type="button"
                  aria-pressed={position >= 0}
                  onClick={() => toggle(creator.name)}
                  className={`rounded-sm border px-2 py-1 text-caption ${
                    position >= 0 ? "border-brand-ink bg-selection font-semibold text-ink" : "border-border-control bg-surface-100 text-ink"
                  }`}
                >
                  {position === 0 ? "1 · rights holder — " : position > 0 ? `${position + 1} · ` : ""}
                  {creator.name}
                </button>
              );
            })}
          </div>
        )}
        {picked.length > 0 ? (
          <p className="mt-1.5 text-caption text-ink-muted">Selected: {creatorLabel(picked)}</p>
        ) : (
          <p className="mt-1.5 text-caption text-ink-subtle">Pick at least the rights holder.</p>
        )}
      </fieldset>

      <label className="mb-3 flex flex-wrap items-center gap-2">
        <span className="font-medium">Owner</span>
        <select
          value={owner}
          onChange={(event) => setOwner(event.target.value)}
          aria-label="Organisation that owns these originals"
          className="rounded-sm border border-border-control bg-surface-100 px-2 py-1 text-caption"
        >
          <option value={OWNER_UNCHOSEN}>Choose an organisation…</option>
          <option value={OWNER_UNASSIGNED}>Unassigned (no customer sees it)</option>
          {(organisations.data ?? []).map((organisation) => (
            <option key={organisation.id} value={String(organisation.id)}>
              {organisation.name}
            </option>
          ))}
        </select>
        <span className="text-caption text-ink-subtle">
          {organisations.isError
            ? `Could not load organisations: ${errorMessage(organisations.error)}`
            : "Never taken from your account: the customer who owns the material."}
        </span>
      </label>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          ref={input}
          type="file"
          multiple
          accept="video/*"
          aria-label="Original video files"
          className="text-caption"
        />
        <Button tone="primary" onClick={() => void start()} disabled={running || picked.length === 0 || organisationId === undefined}>
          {running ? "Hashing…" : "Hash and register"}
        </Button>
        {running ? (
          <Button
            onClick={() => {
              cancel.current.aborted = true;
            }}
          >
            Cancel
          </Button>
        ) : null}
        <span className="text-caption text-ink-subtle">
          {DEFAULT_FPS} frame per second, all 8 orientations. Chrome or Edge, H.264 MP4/MOV.
        </span>
      </div>

      <Flash message={flash} onClose={() => setFlash(null)} />

      {files.length > 0 ? (
        <ul className="mb-3 flex flex-col gap-1.5">
          {files.map((file) => (
            <li key={file.id} className="rounded-sm border border-hairline px-3 py-2">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-medium break-all">{file.name}</span>
                <StatusBadge status={file.status} />
              </div>
              {file.status === "hashing" || file.status === "uploading" ? (
                <div className="mt-1">
                  <div
                    role="meter"
                    aria-valuenow={Math.round(file.progress * 100)}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label={`${file.name} progress`}
                    className="h-1.5 w-full overflow-hidden rounded-full bg-surface-300"
                  >
                    <div className="h-full bg-accent" style={{ width: `${Math.round(file.progress * 100)}%` }} />
                  </div>
                  <p className="mt-1 text-caption text-ink-muted">
                    {file.stage} · {Math.round(file.progress * 100)}%
                  </p>
                </div>
              ) : null}
              {file.status === "done" && file.referenceId !== null ? (
                <p className="mt-1 text-caption text-positive-ink">
                  Registered as{" "}
                  <Link
                    to="/originals/$referenceId"
                    params={{ referenceId: String(file.referenceId) }}
                    className="text-brand-ink underline"
                  >
                    original #{file.referenceId}
                  </Link>{" "}
                  · {file.message}
                </p>
              ) : null}
              {file.status === "failed" ? <p className="mt-1 text-caption text-critical-ink">{file.message}</p> : null}
            </li>
          ))}
        </ul>
      ) : null}

      <details className="text-caption">
        <summary className="cursor-pointer text-ink-muted">
          Import from the command line instead (large batches, ProRes, anything the browser will not decode)
        </summary>
        <p className="mt-1 mb-1 text-ink-muted">
          The CLI stays the supported path for bulk imports; it hashes with ffmpeg and tags{" "}
          <code className="font-mono">ffmpeg-rgb512-pdq-v0</code>. Run matching afterwards with{" "}
          <code className="font-mono">match_references</code>.
        </p>
        <pre className="overflow-x-auto rounded-none border border-hairline bg-surface-inset px-3 py-2 font-mono text-mono text-ink">
          {addReferenceCommand(picked.length ? picked : undefined)}
        </pre>
      </details>
    </section>
  );
}

function OriginalRow({ original }: { original: Original }) {
  return (
    <tr className="border-b border-hairline align-top last:border-0">
      <td className="px-3 py-2">
        <Link to="/originals/$referenceId" params={{ referenceId: String(original.id) }} className="font-medium text-brand-ink underline">
          {original.title}
        </Link>
        <div className="text-caption text-ink-subtle">
          #{original.id} · {creatorLabel(original.creator_names)}
        </div>
      </td>
      <td className="px-3 py-2 tabular-nums">{clock(original.duration_s)}</td>
      <td className="px-3 py-2 tabular-nums">
        {count(original.frames)} ({count(original.shared_frames)})
      </td>
      <td className="px-3 py-2 tabular-nums">
        {count(original.matches_strong)} · {count(original.matches_weak)}
      </td>
      <td className="px-3 py-2 tabular-nums">
        {count(original.matches.proposed)} · {count(original.matches.confirmed)} · {count(original.matches.rejected)}
      </td>
      <td className="px-3 py-2 tabular-nums">
        {count(original.leaks.confirmed)} · {count(original.leaks.rejected)} · {count(original.leaks.proposed)}
      </td>
      <td className="px-3 py-2 tabular-nums">{count(original.linked_clusters)}</td>
    </tr>
  );
}

/** A photo collection has no duration and no sampling rate: it is N images, never a timeline. */
function describe(original: Original): string {
  const frames = `${count(original.frames)} (${count(original.shared_frames)} shared)`;
  if (original.kind === "photos") return `photo collection · ${frames} images`;
  return `${clock(original.duration_s)} · ${frames} frames at ${original.frame_fps ?? "?"} fps`;
}

export function OriginalDetailScreen() {
  const { referenceId } = useParams({ from: "/originals/$referenceId" });
  const id = Number(referenceId);
  const valid = Number.isInteger(id) && id > 0;
  const query = useQuery({
    queryKey: ["original", id],
    queryFn: () => unwrap(api.GET("/api/v1/review/originals/{reference_id}", { params: { path: { reference_id: id } } })),
    enabled: valid,
  });
  if (!valid) return <p className="text-critical-ink">Unknown original.</p>;
  if (query.isPending) return <p className="text-ink-subtle">Loading…</p>;
  if (query.isError) return <p className="text-critical-ink">Could not load: {errorMessage(query.error)}</p>;
  const { original, pairs, m2 } = query.data;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-baseline gap-3">
        <Link to="/originals" className="text-body-s text-brand-ink underline">
          ← Originals
        </Link>
        <h1 className="font-display text-display-m">{original.title}</h1>
        <span className="text-body-s text-ink-muted">
          #{original.id} · {creatorLabel(original.creator_names)} · {describe(original)}
        </span>
        <Link to="/references" search={{ reference: original.id, status: "proposed" }} className="ml-auto text-body-s text-brand-ink underline">
          Review proposed pairs →
        </Link>
      </div>
      <M2Section m2={m2} />
      <PairsTable referenceId={original.id} pairs={pairs} />
    </div>
  );
}

function M2Section({ m2 }: { m2: OriginalM2 }) {
  return (
    <section className="rounded-md border border-hairline bg-surface-100 p-4 text-body-s">
      <h2 className="mb-2 font-semibold">M2 for this original</h2>
      <p className="text-ink-muted">
        {count(m2.confirmed)} confirmed leaks · found strong {count(m2.found_strong)}, weak {count(m2.found_weak)}, linked{" "}
        {count(m2.found_linked)} · false matches {count(m2.false_matches)} of {count(m2.reviewed_matches)} reviewed · distance ≤{" "}
        {m2.max_distance}
      </p>
      {m2.linked.length ? (
        <ul className="mt-2 text-caption text-ink-muted">
          {m2.linked.map((row) => (
            <li key={row.page_id}>
              Page {row.page_id} found via cluster {row.cluster_id} (strong match on page {row.via_page_id})
            </li>
          ))}
        </ul>
      ) : null}
      <h3 className="mt-3 mb-1 font-medium">Confirmed leaks not found</h3>
      {m2.misses.length === 0 ? (
        <p className="text-caption text-ink-muted">None.</p>
      ) : (
        <ul className="flex flex-col gap-1 text-caption">
          {m2.misses.map((miss) => (
            <li key={miss.page_id} className="rounded-sm bg-caution-quiet px-2 py-1 text-caution-ink">
              <span className="font-medium">
                Page {miss.page_id} · {miss.site} · {miss.title || "(no title)"}
              </span>
              <div className="font-mono break-words">{miss.reason}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function PairsTable({ referenceId, pairs }: { referenceId: number; pairs: OriginalPair[] }) {
  if (pairs.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border-control bg-surface-100 p-6 text-center text-ink-muted">
        No matches or leak suggestions for this original.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-md border border-hairline bg-surface-100">
      <table className="w-full text-body-s">
        <thead className="text-left text-caption text-ink-subtle">
          <tr className="border-b border-hairline">
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Page</th>
            <th className="px-3 py-2 font-medium">Availability</th>
            <th className="px-3 py-2 font-medium">Match</th>
            <th className="px-3 py-2 font-medium">Moments</th>
            <th className="px-3 py-2 font-medium">Leak</th>
            <th className="px-3 py-2 font-medium" />
          </tr>
        </thead>
        <tbody>
          {pairs.map((pair) => (
            <tr key={pair.page.id} className="border-b border-hairline align-top last:border-0">
              <td className="px-3 py-2">
                <StatusBadge status={pair.status} />
              </td>
              <td className="px-3 py-2">
                <div className="break-words">{pair.page.title || "(no title)"}</div>
                <div className="text-caption text-ink-subtle">
                  {pair.page.site} · page {pair.page.id}
                  {pair.page.safety_status !== "clear" ? (
                    <span className="ml-1 rounded-sm bg-caution-quiet px-1 text-caution-ink">safety {pair.page.safety_status}</span>
                  ) : null}
                </div>
              </td>
              <td className="px-3 py-2">{pair.page.availability}</td>
              <td className="px-3 py-2 text-caption">
                {pair.match ? (
                  <>
                    <div className="font-semibold">{pair.match.strength}</div>
                    <div>
                      d={pair.match.best_distance} · {pair.match.evidence_frames} evidence frames · {pair.match.status}
                    </div>
                    {pair.match.cluster_id !== null ? <div>cluster {pair.match.cluster_id}</div> : null}
                  </>
                ) : (
                  <span className="text-ink-subtle">no match</span>
                )}
              </td>
              <td className="px-3 py-2 font-mono text-caption">
                {pair.match ? (
                  <>
                    <div>best {pair.match.best_moment}</div>
                    {pair.match.matched_moments.length ? <div>{pair.match.matched_moments.join(", ")}</div> : null}
                    {pair.match.shared_moments.length ? (
                      <div className="text-ink-subtle">shared: {pair.match.shared_moments.join(", ")}</div>
                    ) : null}
                  </>
                ) : (
                  "—"
                )}
              </td>
              <td className="px-3 py-2 text-caption">
                {pair.leak ? (
                  <>
                    <div>{pair.leak.status}</div>
                    <div className="text-ink-subtle">
                      {pair.leak.source} · score {pair.leak.score.toFixed(2)}
                    </div>
                  </>
                ) : (
                  <span className="text-ink-subtle">—</span>
                )}
              </td>
              <td className="px-3 py-2 text-caption whitespace-nowrap">
                <Link
                  to="/references"
                  search={{ reference: referenceId, site: pair.page.site_id, status: statusFilter(pair.status) }}
                  className="text-brand-ink underline"
                >
                  Review →
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
