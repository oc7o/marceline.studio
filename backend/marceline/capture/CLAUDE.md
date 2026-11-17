# Browser capture: the worker (ADR 0016)

The headless-browser half of ADR 0016 lives here; the triage that decides *which* pages are offered
to it stays with matching (`matching/triage.py`, `matching/capture_triage.py`,
`backend/matching/CLAUDE.md`). Split out of `backend/matching/CLAUDE.md` on 2026-09-18 (issue #79);
the full detail is in `docs/conventions/matching.md`. The root `CLAUDE.md` hard rules and
`backend/CLAUDE.md` apply here too.

`marceline/capture/` sits in the project package because one capture needs `crawler`, `matching` and
`index` together, a combination no app may import. It is deliberately **not** in `make workers`: it
runs only while `BROWSER_CAPTURE_ENABLED` is on and inside `BROWSER_CAPTURE_WINDOW_UTC`, and it
needs the Playwright install from `frontend/parity`.

## What may be opened at all
- **`guard.py` is the only place a browser may be pointed at a page.** `capture_page` re-reads
  safety status, score, availability, `Site.enabled` and `Site.capture_enabled` from the database
  *immediately before* the browser starts and raises `CaptureDenied`. **Never call a runner
  directly; the queue filter is not the guarantee.**
- A login, CAPTCHA, paywall or age gate is abandoned with a recorded reason and never worked
  around (`crawler.blocking.detect_block`, ADR 0007); DRM is detected, never decrypted. robots.txt
  and the per-site delay apply before the browser opens.
  - **`html` is capped at 50,000 characters (`capture.mjs` `HTML_LIMIT`) before it reaches
    Python** (message size only), but `classify_age_gate`'s "gate vs. overlay" link count has to be
    the **whole** document's or a normal page is undercounted into a false gate (site id 12, issue
    #78). `capture.mjs` counts `<a href>` before truncating and ships it as `linkCount`;
    `detect_block`/`classify_age_gate` take it as `link_count` instead of recounting on the
    truncated string. **Never recount a capped string** — the HTTP crawler path passes the full
    body and no override.
- **The browser sends the approved age self-declaration cookies** of `Site.age_confirmation` (same
  field, same `catalog.sources.parse_age_confirmation` as the HTTP crawler, which refuses
  session/login/anti-bot cookie names), read fresh per visit and scoped to the site's domain. They
  are the only cookies it ever holds. An age *verification* provider is unchanged: triage excludes
  the site, `detect_block` abandons the visit. Approving a site is a data change, never a code
  change.
- `plan.py` is pure and its caps *are* the cost control. `runner.py` drives `capture.mjs` as a Node
  subprocess and resolves Playwright from the `frontend/parity` harness: **no Python browser
  dependency**, and frames stay in memory. Output is a `Media(kind=capture)` row plus `FrameHash`
  rows through `matching.media.store_hashes`, **which re-checks the gate** — **PDQ hashes only,
  never a frame, screenshot or video.**

## The one click (ADR 0016 amendment 2026-09-18, issue #78)
- **One click on the page's own play control, per site, off by default.**
  `catalog.Site.capture_play_click` is the operator switch (default False, admin-editable, off the
  `SitePolicy`/`seed_sources` path like `capture_enabled`).
- **The whole rule lives in `click.py` and is pure**: `capture.mjs` describes elements,
  `choose_click_target` picks at most one, the browser clicks the index it is handed. **Never add a
  clause to the browser side.**
- An element is admitted only when it does not navigate, sits in the page's own player, is drawn
  >= 24x24 px, comes from a frame the embed allowlist permitted, reads as a play control, and
  carries **no** `FORBIDDEN_TOKENS` token (consent, age, login, subscribe, pay, download, share, ad,
  close, dismiss, **skip**) on itself, its text, its ARIA label or any ancestor — which is why **ad
  skipping is structurally impossible** and a pre-roll still ends the visit with nothing.
- **The click is offered only *after* `detect_block`**, so a gate is abandoned before any
  interaction, and exactly once: `ClickPolicy` spends a budget of 1, `runner._talk` has no loop,
  `capture.mjs` counts too. Still empty afterwards → `diagnosis.NO_PLAYER_AFTER_CLICK` (scanned
  once). **Never a second click, keyboard input or scrolling trick.**
- Recorded on `capture_run.diagnostics.click`: allowed / chosen / performed, the candidate count,
  the kind, the **selector shape** (tag, ARIA role, <= 3 identifier tokens) and whether a player
  existed afterwards. **Never the element's text, ARIA label, title or a URL** — those are read only
  to refuse an element.

## Every request the page makes is asked for (issue #78, ADR 0016 amendment 2026-09-17)
- `capture.mjs` holds it until `requests.RequestPolicy.refusal` answers. **Never let the browser
  send a request past that check.**
- A refusal is reconstructable, never just a count — per host its reason, count and path *shapes*
  (`requests.path_pattern`, **never a full URL**) in `capture_run.diagnostics.refused_hosts`, and a
  visit that never opened a browser on `BrowserCaptureCandidate.refusal_detail` instead, so it stays
  retryable rather than looking "scanned once".
- `capture_worker --refusal-report` answers "which host blocked us" from those rows alone: per host
  the reasons, request counts, path shapes and whether it is already allowlisted. Read-only, no
  browser, and it works even with capture off.
