/**
 * The shared primitives, rendered the way the server first sends them (ADR 0036).
 *
 * They are synchronous server components, so `renderToStaticMarkup` over the real tree is the whole
 * test — no jsdom, no request, no router. What is asserted is what a reviewer would otherwise have
 * to check by hand: the accessible shape (roles, names, `aria-*`), and that every colour is a
 * design-system token rather than a literal, in every state the screens will ask for.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Timeline, type TimelineItem } from "./parts";
import {
  Banner,
  DefinitionList,
  EmptyState,
  Field,
  LinkButton,
  Pagination,
  SkeletonRow,
  StatTile,
  Toolbar,
  Veil,
} from "./ui";

/** Tailwind's own palette, which these components must no longer reach for. */
const LITERAL_COLOUR = /\b(?:bg|text|border|from|via|to)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}\b/;

describe("the primitives", () => {
  it.each([
    ["Field", <Field label="Site">example.test</Field>],
    ["LinkButton", <LinkButton href="/app/originals/new">Add</LinkButton>],
    ["LinkButton (secondary)", <LinkButton href="/app" variant="secondary">Back</LinkButton>],
    ["StatTile", <StatTile label="Originals" value="4" hint="in this account" />],
    ["EmptyState", <EmptyState title="Nothing yet" body="Nothing has turned up." />],
    ["Toolbar", <Toolbar label="Filters">x</Toolbar>],
    [
      "Pagination",
      <Pagination label="Pages" status="1–20 of 57" previous={{ label: "Back" }} next={{ label: "More", href: "/app/removals?page=2" }} />,
    ],
    ["Banner", <Banner tone="caution">Something to know</Banner>],
    ["SkeletonRow", <SkeletonRow label="Loading" />],
  ] as const)("%s uses tokens, never a literal colour", (_name, element) => {
    expect(renderToStaticMarkup(element)).not.toMatch(LITERAL_COLOUR);
  });

  it("gives a stat tile its label, its number and its hint", () => {
    const html = renderToStaticMarkup(<StatTile label="Matching copies" value="12" hint="reviewed by a person" />);
    expect(html).toContain("Matching copies");
    expect(html).toContain("12");
    expect(html).toContain("reviewed by a person");
  });

  it("sets an empty state's headline in the display serif", () => {
    const html = renderToStaticMarkup(<EmptyState title="Nothing yet" />);
    expect(html).toMatch(/font-display[^"]*text-display-m/);
  });

  it("names a toolbar, so its controls are a group with a purpose", () => {
    const html = renderToStaticMarkup(<Toolbar label="Filters">x</Toolbar>);
    expect(html).toContain('role="group"');
    expect(html).toContain('aria-label="Filters"');
  });

  it("makes a pagination step a link only when there is a page there", () => {
    const html = renderToStaticMarkup(
      <Pagination
        label="Pages"
        status="21–40 of 57"
        previous={{ label: "Back", href: "/app/removals?page=1" }}
        next={{ label: "More" }}
      />,
    );
    expect(html).toContain('href="/app/removals?page=1"');
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain("21–40 of 57");
    // The dead end is not a link: a keyboard user never lands on a step that goes nowhere.
    expect(html).not.toMatch(/<a[^>]*>More<\/a>/);
  });

  it.each([
    ["neutral", "bg-surface-200"],
    ["caution", "bg-caution-quiet"],
    ["critical", "bg-critical-quiet"],
    // Violet is the system's mark for anything the machine decided.
    ["automated", "bg-accent-quiet"],
  ] as const)("gives a %s banner its own ground", (tone, ground) => {
    expect(renderToStaticMarkup(<Banner tone={tone}>note</Banner>)).toContain(ground);
  });

  it("announces a skeleton row and hides its bars from the reader", () => {
    const html = renderToStaticMarkup(<SkeletonRow label="Loading the log" lines={3} />);
    expect(html).toContain('role="status"');
    expect(html).toContain('aria-label="Loading the log"');
    expect(html.match(/aria-hidden="true"/g)).toHaveLength(3);
  });

  it("builds a definition list from its fields", () => {
    const html = renderToStaticMarkup(
      <DefinitionList>
        <Field label="Account">Novak Studio</Field>
      </DefinitionList>,
    );
    expect(html).toMatch(/<dl[^>]*>.*<dt[^>]*>Account<\/dt>.*<dd[^>]*>Novak Studio<\/dd>.*<\/dl>/s);
  });
});

describe("the timeline", () => {
  const items: TimelineItem[] = [
    { step: "confirmed", status: "done", title: "Reviewed by a person", meta: "17 Sept 2026" },
    { step: "notice_drafted", status: "pending", title: "Notice draft", meta: "not yet" },
    { step: "acknowledged", status: "unavailable", title: "Response", meta: "not available yet", note: "no channel" },
  ];

  it("is an ordered list, one entry per step", () => {
    const html = renderToStaticMarkup(<Timeline items={items} />);
    expect(html.startsWith("<ol")).toBe(true);
    expect(html.match(/<li /g)).toHaveLength(3);
    expect(html).toContain('data-step="confirmed"');
    expect(html).toContain('data-status="unavailable"');
  });

  it("never lets colour carry the state on its own", () => {
    const html = renderToStaticMarkup(<Timeline items={items} />);
    // Each marker is decoration; the words next to it say what the state is.
    expect(html.match(/aria-hidden="true"/g)).toHaveLength(3);
    for (const item of items) expect(html).toContain(String(item.meta));
  });

  it("uses tokens, never a literal colour", () => {
    expect(renderToStaticMarkup(<Timeline items={items} />)).not.toMatch(LITERAL_COLOUR);
  });
});

describe("the Veil", () => {
  it("is decoration: hidden from the reader, and it catches no pointer", () => {
    const html = renderToStaticMarkup(<Veil />);
    expect(html).toContain('aria-hidden="true"');
    expect(html).toContain("pointer-events-none");
  });

  it("is three blurred planes on the veil tokens, and no literal colour", () => {
    const html = renderToStaticMarkup(<Veil />);
    expect(html).toContain("bg-veil-brand");
    expect(html).toContain("bg-veil-accent");
    expect(html).toContain("bg-veil-ink");
    expect(html.match(/blur-veil/g)).toHaveLength(3);
    expect(html).not.toMatch(LITERAL_COLOUR);
  });

  it("does not animate — the system's rule, so reduced motion has nothing to undo", () => {
    expect(renderToStaticMarkup(<Veil />)).not.toMatch(/animate-|transition/);
  });

  it("renders nothing that could load an image (#53)", () => {
    expect(renderToStaticMarkup(<Veil />)).not.toMatch(/<(img|image|picture|source|video)\b/i);
  });

  it("is drawn behind an empty state only when the screen asks for one", () => {
    expect(renderToStaticMarkup(<EmptyState title="Nothing yet" />)).not.toContain("bg-veil-brand");
    expect(renderToStaticMarkup(<EmptyState title="Nothing yet" veil />)).toContain("bg-veil-brand");
  });

  it("lets an empty state be one sentence with no invented headline", () => {
    const html = renderToStaticMarkup(<EmptyState body="Nothing has been removed yet." />);
    expect(html).toContain("Nothing has been removed yet.");
    expect(html).not.toContain("text-display-m");
  });
});
