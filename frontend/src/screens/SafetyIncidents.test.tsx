// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Schemas } from "../api/client";
import { ReportDraftView } from "./SafetyIncidents";

afterEach(cleanup);

const DRAFT: Schemas["ReportDraftOut"] = {
  incident_id: 4,
  reference_id: "MARCELINE-SI-4",
  status: "open",
  template_version: "hotline-report-v1",
  channel: "internet-beschwerdestelle.de (eco/FSM complaints web form)",
  supplement: false,
  reported_at: null,
  reference: "",
  media_fetch: "none",
  urls: [
    {
      url: "https://example.test/video/a",
      site: "example.test",
      evidence: "origin",
      primary: true,
      first_seen_at: "2026-09-16T08:30:00Z",
      last_seen_at: "2026-09-16T08:30:00Z",
      new_since_report: false,
    },
    {
      url: "https://mirror.example.test/video/b",
      site: "mirror.example.test",
      evidence: "mirror",
      primary: false,
      first_seen_at: "2026-09-16T10:30:00Z",
      last_seen_at: "2026-09-16T10:30:00Z",
      new_since_report: false,
    },
  ],
  drafts: [
    { language: "de", subject: "Maschinell erzeugter Verdacht", body: "Guten Tag,\n\nURL:\nhttps://example.test/video/a\n" },
    { language: "en", subject: "Machine-generated suspicion", body: "Hello,\n\nURL:\nhttps://example.test/video/a\n" },
  ],
};

describe("ReportDraftView", () => {
  it("shows the German draft first and switches to English", () => {
    render(<ReportDraftView draft={DRAFT} onCopy={() => undefined} />);

    expect(screen.getByDisplayValue("Maschinell erzeugter Verdacht")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "English" }));
    expect(screen.getByDisplayValue("Machine-generated suspicion")).toBeTruthy();
    expect(screen.getByRole("button", { name: "English" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("copies the URL, the subject and the full text", () => {
    const onCopy = vi.fn();
    render(<ReportDraftView draft={DRAFT} onCopy={onCopy} />);

    fireEvent.click(screen.getByRole("button", { name: "Copy URL https://mirror.example.test/video/b" }));
    fireEvent.click(screen.getByRole("button", { name: "Copy subject" }));
    fireEvent.click(screen.getByRole("button", { name: "Copy subject + text" }));

    expect(onCopy.mock.calls).toEqual([
      ["https://mirror.example.test/video/b"],
      ["Maschinell erzeugter Verdacht"],
      ["Maschinell erzeugter Verdacht\n\nGuten Tag,\n\nURL:\nhttps://example.test/video/a\n"],
    ]);
  });

  it("never renders a link, an image or any element that loads something", () => {
    const { container } = render(<ReportDraftView draft={{ ...DRAFT, supplement: true, reported_at: "2026-09-16T12:00:00Z" }} onCopy={() => undefined} />);

    expect(container.querySelectorAll("a, img, video, iframe, source, picture, object, embed")).toHaveLength(0);
    expect(container.textContent).toContain("Already reported");
    expect(container.textContent).toContain("no reference number issued");
  });
});
