"use client";
/**
 * Choosing what to fingerprint — one video, or the photographs of one set (`multiple`, ADR 0053):
 * the ordinary `<input type="file">`, with a drop target drawn around it.
 *
 * **The file never leaves the computer** (issue #29): this hands a `File` to the caller, which
 * hashes it in a Worker and uploads hashes only. Nothing here reads, previews or renders the
 * picture — the customer app is incapable of showing one (issue #53), and a drop target is the one
 * place that rule is easy to break by accident. It stays that way: no object URL, no preview.
 *
 * An island, because dragging is the browser's. The input stays visible and keeps the screen's own
 * `<label htmlFor>`, so the keyboard and screen-reader path is the plain one browsers already
 * handle and the drop target is only an addition to it.
 *
 * Every word arrives as a prop, already in the customer's language.
 */
import { useId, useState } from "react";

interface FileDropBase {
  /** The input's id, so the screen's own `<label htmlFor>` still points at it. */
  id: string;
  accept?: string;
  disabled?: boolean;
  describedBy?: string;
  /** The instruction inside the target ("Drop a video here, or choose one"). */
  label: string;
  hint?: string;
  /** What was chosen, in the screen's words — a count or a name and a size, never a preview. */
  chosen?: string;
}

/**
 * One file (a video) or many (the photographs of one set, ADR 0053). A discriminated union rather
 * than a second callback: a caller of the multiple picker cannot then read "the file", or the
 * other way round.
 */
export type FileDropProps = FileDropBase &
  (
    | { multiple?: false; onChoose: (file: File | null) => void }
    | { multiple: true; onChoose: (files: File[]) => void }
  );

export function FileDrop(props: FileDropProps) {
  const { id, accept, disabled = false, describedBy, label, hint, chosen } = props;
  const [over, setOver] = useState(false);
  const hintId = useId();
  const described = [describedBy, hint === undefined ? undefined : hintId]
    .filter((value): value is string => Boolean(value))
    .join(" ");

  const take = (files: FileList | null) => {
    if (disabled) return;
    const chosenFiles = files === null ? [] : [...files];
    if (props.multiple) props.onChoose(chosenFiles);
    else props.onChoose(chosenFiles[0] ?? null);
  };

  return (
    <div
      data-over={over ? "" : undefined}
      onDragOver={(event) => {
        if (disabled) return;
        event.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        setOver(false);
        take(event.dataTransfer.files);
      }}
      className={`flex flex-col items-start gap-2 rounded-md border border-dashed p-4 transition-colors duration-120 ease-standard ${
        over ? "border-brand bg-brand-quiet" : "border-hairline-strong bg-surface-inset"
      } ${disabled ? "opacity-45" : ""}`}
    >
      <p className="text-body-s text-ink">{label}</p>
      {hint === undefined ? null : (
        <p id={hintId} className="text-caption text-ink-subtle">
          {hint}
        </p>
      )}
      <input
        id={id}
        type="file"
        accept={accept}
        multiple={props.multiple === true}
        disabled={disabled}
        aria-describedby={described === "" ? undefined : described}
        onChange={(event) => take(event.target.files)}
        className="max-w-full text-body-s text-ink file:mr-3 file:rounded-sm file:border file:border-border-control file:bg-surface-100 file:px-3 file:py-1.5 file:text-button file:text-ink hover:file:bg-surface-300"
      />
      {chosen === undefined ? null : (
        <p className="font-mono text-mono-s break-all text-ink-muted">{chosen}</p>
      )}
    </div>
  );
}
