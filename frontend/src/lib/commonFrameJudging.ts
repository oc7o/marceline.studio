/**
 * Pure helpers for the common-frame labeling screen's "which frame is the same" problem
 * (operator, 2026-09-18: "I couldn't review the group because I don't know which frames are the
 * same"). Framework-free so they are testable without rendering anything.
 */
import type { Schemas } from "../api/client";
import { clock } from "./format";

export type MemberFrame = Schemas["CommonFrameMemberFrameOut"];
export type MemberPage = Schemas["CommonFrameMemberPageOut"];

/** Why the passthrough (`GET /media/{id}/image`) cannot show this one frame alone. */
const NOT_SHOWABLE_REASON: Record<string, string> = {
  sprite: "part of a sprite sheet — several frames share one image",
  preview: "part of a preview clip — several frames share one file",
  capture: "part of a browser capture — several frames share one file",
};

export function notShowableReason(kind: string): string {
  return NOT_SHOWABLE_REASON[kind] ?? "this media row holds more than one frame";
}

/**
 * "frame 3 of 8 at 00:02 · distance 5 bits" — the member frame's own order (when the site's own
 * numbering or a fixed sampling rate gives one), how many frames the page holds in total, and its
 * stored Hamming distance to the group's representative hash. A poster-shaped member has no order
 * at all (`position` null): say only what is true.
 */
export function memberFrameLabel(frame: MemberFrame, frameCount: number): string {
  const distanceText = `distance ${frame.distance} bit${frame.distance === 1 ? "" : "s"}`;
  if (frame.position === null) {
    return `this page's repeated frame · ${distanceText}`;
  }
  const of = frameCount > 0 ? ` of ${frameCount}` : "";
  const at = frame.timestamp_s !== null ? ` at ${clock(frame.timestamp_s)}` : "";
  return `frame ${frame.position}${of}${at} · ${distanceText}`;
}

/** A page whose gated/ungated member frames are all unshowable, or which has none at all. */
export function showableMemberFrame(page: MemberPage): MemberFrame | null {
  return page.member_frames.find((frame) => frame.showable) ?? null;
}

interface Bucket {
  value: number | null;
  count: number;
}

function bucket(values: (number | null)[]): Bucket[] {
  const counts = new Map<number | null, number>();
  for (const value of values) {
    counts.set(value, (counts.get(value) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => (b.count - a.count) || ((a.value ?? -1) - (b.value ?? -1)));
}

export interface AllGatedFacts {
  pageCount: number;
  durations: Bucket[];
  distinctTitleCount: number;
  frameCounts: Bucket[];
  siteCount: number;
}

/**
 * What is left to judge a group by when every sample page is gated or safety-flagged and no
 * image can be shown at all: the stored numbers, never the titles themselves.
 */
export function summarizeAllGated(pages: readonly MemberPage[]): AllGatedFacts {
  const titles = new Set(
    pages.map((page) => page.page.title.trim()).filter((title) => title.length > 0),
  );
  const sites = new Set(pages.map((page) => page.page.site));
  return {
    pageCount: pages.length,
    durations: bucket(pages.map((page) => page.page.duration_s)),
    distinctTitleCount: titles.size,
    frameCounts: bucket(pages.map((page) => page.frame_count)),
    siteCount: sites.size,
  };
}

function durationLabel(value: number | null): string {
  return value === null ? "no duration stored" : clock(value);
}

/** One line per fact, for a bullet list; never a title or a page's own text. */
export function allGatedFactLines(facts: AllGatedFacts): string[] {
  return [
    `${facts.pageCount} sample page${facts.pageCount === 1 ? "" : "s"}, ${facts.siteCount} site${
      facts.siteCount === 1 ? "" : "s"
    }`,
    `Durations: ${facts.durations
      .map((b) => `${durationLabel(b.value)} (×${b.count})`)
      .join(", ")}`,
    `Distinct titles: ${facts.distinctTitleCount}`,
    `Frames per page: ${facts.frameCounts.map((b) => `${b.value} (×${b.count})`).join(", ")}`,
  ];
}

/** How to read the facts above, in the labeling screen's own words. */
export const ALL_GATED_READING =
  "One duration and one or two frames per page usually means the same upload reposted. Many " +
  "durations and titles with exactly one shared frame among several usually means an intro or a " +
  "logo.";
