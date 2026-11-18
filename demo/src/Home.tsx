/**
 * The demo's front page, which the product never had: what this was, the three plans as the design
 * system draws them, and the way into the app. The links into `/app` are plain anchors on purpose —
 * the app has to be loaded as a document from its own path to read its language cookie.
 */
import { Veil } from "../../frontend/customer/components/ui";
import { type Tier, TierIcon } from "./TierIcon";
import { BASE_PATH } from "./runtime/paths";

const ARTICLE_URL = "https://sloow.de/posts/five-days-1734-commits-zero-customers/";

const TIERS: { tier: Tier; name: string; drawing: string }[] = [
  { tier: "pro", name: "Pro", drawing: "A pair of upper fangs. It is also the logo." },
  { tier: "max", name: "Max", drawing: "The lower pair joins them." },
  { tier: "ultra", name: "Ultra", drawing: "A small tooth either side of both." },
];

const BUTTON = "tap inline-flex items-center gap-1 rounded-sm border px-4 py-2 text-button transition-colors duration-120 ease-standard";

export function Home() {
  return (
    <main className="bg-surface-000 text-ink">
      <section className="relative isolate overflow-hidden border-b border-hairline">
        <Veil />
        <div className="mx-auto flex max-w-5xl flex-col items-start gap-6 px-4 py-16 sm:px-8 sm:py-24">
          <span className="inline-flex items-center gap-3">
            <TierIcon tier="pro" size={20} />
            <span className="font-display text-display-s">
              Marceline <span className="text-brand-ink">Studio</span>
            </span>
          </span>
          <h1 className="max-w-3xl font-display text-display-l sm:text-display-xl">
            Finds the copies of your videos, and writes the takedown notice.
          </h1>
          <p className="max-w-xl text-body-l text-ink-muted">
            Leak detection for content creators, built by coding agents in five days. It found no customers and was
            sunset before launch. What is left is the code, the design and this demo.
          </p>
          <div className="flex flex-wrap gap-3">
            <a href={`${BASE_PATH}/`} className={`${BUTTON} border-brand bg-brand text-on-brand shadow-sm hover:bg-brand-hover`}>
              Open the dashboard
            </a>
            <a
              href={ARTICLE_URL}
              target="_blank"
              rel="noopener noreferrer"
              className={`${BUTTON} border-border-control bg-surface-100 text-ink hover:bg-surface-300`}
            >
              Read the story
            </a>
          </div>
        </div>
      </section>

      <section aria-labelledby="plans" className="mx-auto max-w-5xl px-4 py-16 sm:px-8 sm:py-20">
        <p className="text-overline text-ink-subtle uppercase">The plans</p>
        <h2 id="plans" className="mt-2 max-w-2xl font-display text-display-m sm:text-display-l">
          One drawing, at three counts.
        </h2>
        <p className="mt-4 max-w-xl text-body text-ink-muted">
          A plan is drawn in the same hand as the brand: every step up adds teeth.
        </p>
        <ul className="mt-10 grid gap-4 sm:grid-cols-3">
          {TIERS.map(({ tier, name, drawing }) => (
            <li key={tier} className="flex flex-col gap-4 rounded-md border border-hairline bg-surface-100 p-6">
              <div className="flex h-40 items-center justify-center">
                <TierIcon tier={tier} size={64} />
              </div>
              <h3 className="font-display text-display-s">{name}</h3>
              <p className="text-body-s text-ink-muted">{drawing}</p>
            </li>
          ))}
        </ul>
        <p className="mt-10 text-body text-ink-muted">
          The application itself is behind the dashboard, with sample data.{" "}
          <a href={`${BASE_PATH}/`} className="tap text-brand-ink underline underline-offset-2">
            Open the dashboard
          </a>
        </p>
      </section>
    </main>
  );
}
