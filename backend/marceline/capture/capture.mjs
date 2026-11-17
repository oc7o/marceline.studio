// Sample frames from the video player on one page, in a real headless browser.
//
// Driven as a subprocess by `marceline/capture/runner.py`; it speaks one JSON line at a time on
// stdin/stdout and writes every diagnostic to stderr. Playwright is resolved from the harness
// install (`frontend/parity/node_modules`, see its README) so the backend needs no Python
// browser dependency and `npm ci` never pulls a browser into the app.
//
// Protocol (one JSON object per line):
//   in   {url, userAgent, engine, executablePath, blockedResourceTypes,
//         plan:{maxFrames,maxSeconds,maxBytes,timeoutMs}, viewport:{width,height}}
//   out  {event:"check", id, url, type, subframe}      before every request the page makes
//   in   {action:"check", id, allow}                   the Python request policy's answer
//   out  {event:"loaded", status, url, title, html, linkCount, video:{...}, videos:[...],
//         playControl, players, clickCandidates:[...], clicked}
//   in   {action:"capture", positions:[seconds…]} | {action:"abort", reason}
//                                                 | {action:"click", index}   (at most once)
//   out  {event:"done", frames:[{position, png}], stats:{…}, skipReason}
//
// Hard rules this file must keep:
//   * No login, no CAPTCHA solving, no age-verification click-through, no anti-bot evasion.
//   * **At most ONE click per visit, and only on the element Python named** (ADR 0016 amendment
//     2026-09-18, issue #78). The rule for *which* element that may be lives entirely in
//     `marceline/capture/click.py`; this file only describes elements and clicks the index it is
//     handed back. The click is offered only after the loaded page has been judged by the
//     crawler's own block/challenge/age-gate detector, so a gate is abandoned before any
//     interaction. No second click, no keyboard input, no scrolling trick, no ad dismissal — a
//     "skip ad" control can never be named, because the policy module refuses it.
//   * No request leaves the browser before the Python policy allowed it (robots.txt per host,
//     embed-host allowlist; marceline/capture/requests.py).
//   * Nothing is written to disk: frames go back as base64 in memory and the caller drops them
//     after hashing. No screenshots saved, no video recording, no HAR, no downloads.
//   * DRM is detected and abandoned, never decrypted.
//   * The only cookies ever set are the operator-approved age self-declaration of
//     `Site.age_confirmation` (ADR 0007's amendment), handed over by the worker. Never a session,
//     login or anti-bot cookie: `catalog.sources.parse_age_confirmation` refuses those by name
//     long before they reach this file.

import { createRequire } from "node:module";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";

// The size cap on what this file ships to Python: bounds the JSON message over the pipe, since a
// rendered DOM can run to hundreds of KB and `html`'s only consumer (`crawler.blocking.detect_block`
// via marceline.capture.worker) never needs more than this to recognise a block, challenge, login
// or age-gate page. `classify_age_gate` also compares the page's *whole* `<a href>` count against a
// threshold (issue #78): truncating before counting undercounts it and misclassifies an ordinary
// page — the layout class an unrelated wrapper happens to share with a real gate — as a gate, so
// `LINK_PATTERN` is applied to the full page below, before the slice, and shipped alongside it.
const HTML_LIMIT = 50_000;
const LINK_PATTERN = /<a\s[^>]*href/gi;
const SEEK_TIMEOUT_MS = 15_000;
// Long enough for a player to initialise while every new host's robots.txt is read first.
const VIDEO_WAIT_MS = 20_000;
const VISIBLE_WAIT_MS = 5_000;
const CLICK_TIMEOUT_MS = 10_000;
// How long a player gets to appear after the one allowed click before we give up on it.
const AFTER_CLICK_WAIT_MS = 20_000;
// Elements offered to the policy module. Bounded on purpose: a page has thousands of divs and
// describing them all would cost more than the visit. This selector names controls.
const CLICK_SELECTOR = [
  "video",
  "button",
  '[role="button"]',
  '[class*="play" i]',
  '[id*="play" i]',
  '[aria-label*="play" i]',
  '[title*="play" i]',
].join(", ");
const MAX_CLICK_CANDIDATES = 40;
// Ancestor shapes described per candidate; `click.ANCESTOR_DEPTH` is the same number.
const ANCESTOR_DEPTH = 6;
const TEXT_LIMIT = 80;
const IDENTIFIER_LIMIT = 120;

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function note(text) {
  process.stderr.write(`${text}\n`);
}

/** Playwright from the parity harness install; never a copy of its own. */
function loadPlaywright() {
  const dir = process.env.MARCELINE_PLAYWRIGHT_DIR;
  if (!dir) throw new Error("MARCELINE_PLAYWRIGHT_DIR is not set");
  const require = createRequire(path.join(dir, "anchor.cjs"));
  return require("playwright");
}

/** The engine BROWSER_CAPTURE_ENGINE names. No launch flag changes what the browser claims to be. */
async function launchBrowser(job) {
  const playwright = loadPlaywright();
  const options = { headless: true };
  if (job.executablePath) options.executablePath = job.executablePath;
  switch (job.engine || "chromium") {
    case "chromium":
      return playwright.chromium.launch(options);
    case "chrome":
      return playwright.chromium.launch(
        job.executablePath ? options : { ...options, channel: "chrome" },
      );
    case "firefox":
      return playwright.firefox.launch(options);
    default:
      throw new Error(`unknown browser engine ${job.engine}`);
  }
}

/** Lines from stdin: request-check answers go to their waiter, everything else to `next()`. */
function readMessages() {
  const reader = readline.createInterface({ input: process.stdin });
  const pending = [];
  const waiting = [];
  const checks = new Map();
  let closed = false;
  reader.on("line", (line) => {
    if (!line.trim()) return;
    const message = JSON.parse(line);
    if (message.action === "check") {
      const resolve = checks.get(message.id);
      checks.delete(message.id);
      if (resolve) resolve(Boolean(message.allow));
      return;
    }
    if (waiting.length) waiting.shift()(message);
    else pending.push(message);
  });
  reader.on("close", () => {
    closed = true;
    while (waiting.length) waiting.shift()(null);
    // No policy left to ask: refuse everything still waiting.
    for (const resolve of checks.values()) resolve(false);
    checks.clear();
  });
  let nextId = 0;
  return {
    next: () =>
      new Promise((resolve) => {
        if (pending.length) resolve(pending.shift());
        else if (closed) resolve(null);
        else waiting.push(resolve);
      }),
    ask: (request) =>
      new Promise((resolve) => {
        if (closed) return resolve(false);
        nextId += 1;
        checks.set(nextId, resolve);
        send({ event: "check", id: nextId, ...request });
      }),
  };
}

/** Text-only state of every <video> in a frame, plus hints for a skip reason. */
function frameVideos() {
  const kind = (src) => {
    if (!src) return "none";
    if (src.startsWith("blob:")) return "blob";
    const pathname = src.split("?")[0].toLowerCase();
    if (pathname.endsWith(".m3u8")) return "hls";
    if (pathname.endsWith(".mpd")) return "dash";
    if (pathname.includes(".mp4")) return "mp4";
    if (pathname.endsWith(".webm")) return "webm";
    return "other";
  };
  const videos = [...document.querySelectorAll("video")].map((video, index) => {
    const rect = video.getBoundingClientRect();
    return {
      index,
      duration: Number.isFinite(video.duration) && video.duration > 0 ? video.duration : null,
      readyState: video.readyState,
      networkState: video.networkState,
      error: video.error ? video.error.code : null,
      rendered: [Math.round(rect.width), Math.round(rect.height)],
      srcKind: kind(video.currentSrc || video.getAttribute("src") || ""),
      width: video.videoWidth || null,
      height: video.videoHeight || null,
      // Read-only DRM probe. We never construct or request media keys.
      drm: video.mediaKeys != null,
    };
  });
  const players = [
    ["jwplayer", "jwplayer"],
    ["videojs", "video.js"],
    ["Plyr", "plyr"],
    ["Clappr", "clappr"],
    ["flowplayer", "flowplayer"],
    ["fluidPlayer", "fluidplayer"],
    ["kt_player", "kvs"],
    ["Playerjs", "playerjs"],
    ["Hls", "hls.js"],
    ["dashjs", "dash.js"],
    ["shaka", "shaka"],
  ]
    .filter(([global]) => typeof window[global] !== "undefined")
    .map(([, name]) => name);
  const playControl = Boolean(
    document.querySelector(
      '[class*="play" i]:not(video), [id*="play" i]:not(video), [aria-label*="play" i]',
    ),
  );
  return { videos, players, playControl };
}

/** Every element that could be a play control, described for `marceline/capture/click.py`.
 *
 * Runs inside one frame. It decides **nothing**: it reports the tag, the identifiers, the ARIA
 * label, the visible text, the drawn box and four structural facts (drawn, navigates, a <video>
 * at or near it, is itself a <video>) plus the shapes of up to `ANCESTOR_DEPTH` ancestors. The
 * pure policy module then picks at most one of them — or none. The elements themselves are parked
 * on `window.__marcelineClickCandidates` so the chosen one can be clicked without a selector
 * round-trip, which would be a second, racy lookup.
 */
function frameClickCandidates([selector, maxCandidates, textLimit, identifierLimit, depth]) {
  const identifiers = (el) => {
    const id = String(el.id || "").slice(0, identifierLimit);
    const cls = (typeof el.className === "string" ? el.className : "").slice(0, identifierLimit);
    return { id, cls };
  };
  const shape = (el) => {
    const { id, cls } = identifiers(el);
    return `${el.tagName.toLowerCase()}${id ? `#${id}` : ""}${cls ? `.${cls}` : ""}`;
  };
  const drawn = (el, rect) => {
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(el);
    return (
      style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) > 0.1
    );
  };
  const nearVideo = (el) => {
    let node = el;
    for (let level = 0; node && level <= depth; level += 1) {
      if (node.tagName === "VIDEO" || node.querySelector("video")) return true;
      node = node.parentElement;
    }
    return false;
  };
  window.__marcelineClickCandidates = [];
  const seen = [...document.querySelectorAll(selector)].map((el) => {
    const rect = el.getBoundingClientRect();
    return { el, rect, area: rect.width * rect.height };
  });
  seen.sort((a, b) => b.area - a.area);
  const described = [];
  for (const { el, rect } of seen.slice(0, maxCandidates)) {
    const ancestors = [];
    let parent = el.parentElement;
    for (let level = 0; parent && level < depth; level += 1) {
      ancestors.push(shape(parent));
      parent = parent.parentElement;
    }
    const { id, cls } = identifiers(el);
    window.__marcelineClickCandidates.push(el);
    described.push({
      tag: el.tagName.toLowerCase(),
      id,
      className: cls,
      role: String(el.getAttribute("role") || "").slice(0, 40),
      ariaLabel: String(el.getAttribute("aria-label") || "").slice(0, textLimit),
      title: String(el.getAttribute("title") || "").slice(0, textLimit),
      text: String(el.textContent || "").trim().slice(0, textLimit),
      rendered: [Math.round(rect.width), Math.round(rect.height)],
      visible: drawn(el, rect),
      // A click that navigates or submits is never "press play".
      navigates: el.closest("a[href], form, input[type=submit], button[type=submit]") !== null,
      insidePlayer: nearVideo(el),
      isVideo: el.tagName === "VIDEO",
      ancestors,
    });
  }
  return described;
}

/** Click candidates from the main frame and from subframes we were allowed to load.
 *
 * An ad or pop-under iframe never gets here: its document request was refused by the embed-host
 * allowlist, so `allowedFrame` does not know it. Returns the descriptions for Python and, beside
 * them, where each one lives, so the answer's index addresses exactly one element.
 */
async function collectClickCandidates(page, allowedFrame) {
  const descriptions = [];
  const targets = [];
  for (const frame of page.frames()) {
    if (!allowedFrame(frame)) continue;
    let found = [];
    try {
      found = await frame.evaluate(frameClickCandidates, [
        CLICK_SELECTOR,
        MAX_CLICK_CANDIDATES,
        TEXT_LIMIT,
        IDENTIFIER_LIMIT,
        ANCESTOR_DEPTH,
      ]);
    } catch {
      continue; // a frame can be detached mid-navigation
    }
    found.forEach((item, at) => {
      descriptions.push({ ...item, frameAllowed: true });
      targets.push({ frame, at });
    });
  }
  return { descriptions, targets };
}

/** The player to sample: across all frames, the largest drawn <video> with a usable duration.
 *
 * The <video> element almost always exists in the markup long before its metadata has loaded, so
 * finding the element is not the same as knowing how long the video is. Polling stops once a
 * video has a finite duration; if the wait runs out we still report every element we saw, so the
 * caller can tell "no player at all" from "a player that never reported a duration". Choosing by
 * drawn size rather than taking the first element skips ad and preview videos next to the player.
 */
async function findVideo(page, waitMs = VIDEO_WAIT_MS) {
  const deadline = Date.now() + waitMs;
  let seen = { best: null, videos: [], players: [], playControl: false };
  while (Date.now() < deadline) {
    const round = { best: null, videos: [], players: new Set(), playControl: false };
    for (const frame of page.frames()) {
      try {
        const info = await frame.evaluate(frameVideos);
        info.players.forEach((name) => round.players.add(name));
        round.playControl ||= info.playControl;
        for (const video of info.videos) {
          round.videos.push(video);
          if (video.duration === null) continue;
          const area = video.rendered[0] * video.rendered[1];
          const bestArea = round.best ? round.best.info.rendered[0] * round.best.info.rendered[1] : -1;
          if (area > bestArea) round.best = { frame, index: video.index, info: video };
        }
      } catch {
        // A frame can be detached mid-navigation; try the next one.
      }
    }
    seen = { ...round, players: [...round.players] };
    if (round.best) return seen;
    await page.waitForTimeout(500);
  }
  return seen;
}

/** Start playback muted, then pause: many players refuse a programmatic seek before the stream is
 * running, and some keep the player hidden until it plays. No click: `play()` is called on the
 * element. Pausing matters: a *playing* video moves on while the screenshot is taken, and the
 * picture captured was measured up to a second away from the seek target (issue #78). */
async function startPlayback(frame, index) {
  await frame.evaluate(async (at) => {
    const video = document.querySelectorAll("video")[at];
    if (!video) return;
    video.muted = true;
    video.setAttribute("playsinline", "");
    try {
      await video.play();
    } catch {
      // Autoplay refused; seeking alone still renders frames in most players.
    }
    video.pause();
  }, index);
}

/** Seek to `position` and wait until that picture is actually on screen. */
async function seekTo(frame, index, position) {
  return frame.evaluate(
    async ([at, seconds, timeout]) => {
      const video = document.querySelectorAll("video")[at];
      if (!video) return false;
      return await new Promise((resolve) => {
        let settled = false;
        const finish = (ok) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          video.removeEventListener("seeked", onSeeked);
          resolve(ok);
        };
        const timer = setTimeout(() => finish(false), timeout);
        const onRendered = () => finish(true);
        const onSeeked = () => {
          // A paused video presents the sought frame once; not every engine reports that through
          // requestVideoFrameCallback, so two animation frames are the fallback either way.
          if (typeof video.requestVideoFrameCallback === "function") {
            video.requestVideoFrameCallback(onRendered);
          }
          requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(onRendered, 50)));
        };
        video.addEventListener("seeked", onSeeked, { once: true });
        try {
          video.currentTime = seconds;
        } catch {
          finish(false);
        }
      });
    },
    [index, position, SEEK_TIMEOUT_MS],
  );
}

async function run(job) {
  const started = Date.now();
  const stats = { wallMs: 0, sampleMs: 0, bytes: 0, requests: 0, abortedRequests: 0, browser: "" };
  const blockedTypes = new Set(job.blockedResourceTypes || []);
  let overBudget = false;
  // One click per visit, counted here as well as in Python (`click.MAX_CLICKS_PER_VISIT`).
  let clicksUsed = 0;

  const browser = await launchBrowser(job);
  stats.browser = `${job.engine || "chromium"} ${browser.version()}`;
  const context = await browser.newContext({
    userAgent: job.userAgent,
    viewport: job.viewport,
    acceptDownloads: false,
    javaScriptEnabled: true,
    serviceWorkers: "block",
  });
  // Subframe documents the policy allowed: only those frames may offer a click candidate, so an
  // ad or pop-under iframe can never contribute one (its document was refused).
  const allowedSubframes = new Set();
  const refuse = (route) => {
    stats.abortedRequests += 1;
    route.abort().catch(() => {});
  };
  await context.route("**/*", async (route, request) => {
    const type = request.resourceType();
    // Decoration and ads by type need no question; the over-budget cap refuses everything.
    if (overBudget || blockedTypes.has(type)) return refuse(route);
    let subframe = false;
    try {
      subframe = type === "document" && request.frame().parentFrame() !== null;
    } catch {
      subframe = false;
    }
    const allowed = await job.messages.ask({ url: request.url(), type, subframe });
    if (!allowed || overBudget) return refuse(route);
    if (subframe) allowedSubframes.add(request.url());
    route.continue().catch(() => {});
  });
  // Bytes are counted per request so that a response still streaming when the visit ends counts
  // too: `response` records its declared Content-Length, and `requestfinished` later replaces
  // that with what was actually transferred. Without the first half a video that never finishes
  // downloading — the normal case when we stop sampling — would read as costing nothing.
  const transferred = new Map();
  const addBytes = (request, value) => {
    stats.bytes += value - (transferred.get(request) || 0);
    transferred.set(request, value);
    if (stats.bytes > job.plan.maxBytes) overBudget = true;
  };
  context.on("response", (response) => {
    const declared = Number(response.headers()["content-length"] || 0);
    if (declared > 0) addBytes(response.request(), declared);
  });
  context.on("requestfinished", (request) => {
    stats.requests += 1;
    request
      .sizes()
      .then((sizes) => {
        addBytes(request, (sizes.responseBodySize || 0) + (sizes.responseHeadersSize || 0));
      })
      .catch(() => {});
  });

  // The only cookies this browser ever holds: the operator-approved age self-declaration of
  // `Site.age_confirmation`, already validated on the Python side (never a session, login or
  // anti-bot cookie). An empty list is the normal case.
  if (Array.isArray(job.cookies) && job.cookies.length) {
    await context
      .addCookies(job.cookies)
      .catch((error) => note(`age confirmation cookies refused: ${error.message}`));
  }

  const page = await context.newPage();
  page.setDefaultTimeout(job.plan.timeoutMs);
  let drmSeen = false;
  // Pop-unders are an ad technique, not content: close them instead of interacting with them.
  // Only *extra* pages — this event fires for the page above as well, and closing that one
  // aborts our own navigation.
  context.on("page", (extra) => {
    if (extra !== page) extra.close().catch(() => {});
  });
  // An `encrypted` event means the stream is DRM protected. The listener has to be registered
  // before navigating or it never runs for this load. We only record it and leave; a protected
  // stream is abandoned, never decrypted.
  await page
    .addInitScript(() => {
      document.addEventListener(
        "encrypted",
        () => {
          window.__marcelineEncrypted = true;
        },
        true,
      );
    })
    .catch(() => {});

  try {
    let response = null;
    try {
      response = await page.goto(job.url, { waitUntil: "domcontentloaded" });
    } catch (error) {
      return { frames: [], stats, skipReason: `navigation_failed: ${error.message}`.slice(0, 300) };
    }

    const allowedFrame = (frame) =>
      frame === page.mainFrame() || allowedSubframes.has(frame.url());
    const report = async (found, extra) => {
      const best = found.best;
      const fullHtml = await page.content().catch(() => "");
      const linkCount = (fullHtml.match(LINK_PATTERN) || []).length;
      const html = fullHtml.slice(0, HTML_LIMIT);
      const encrypted = await page
        .evaluate(() => Boolean(window.__marcelineEncrypted))
        .catch(() => false);
      drmSeen = encrypted || found.videos.some((video) => video.drm);
      send({
        event: "loaded",
        status: response ? response.status() : 0,
        url: page.url(),
        title: await page.title().catch(() => ""),
        html,
        linkCount,
        video: {
          found: Boolean(best),
          duration: best?.info.duration ?? null,
          drm: drmSeen,
          width: best?.info.width ?? null,
          height: best?.info.height ?? null,
          rendered: best?.info.rendered ?? [0, 0],
        },
        videos: found.videos,
        playControl: found.playControl,
        players: found.players,
        ...extra,
      });
      return (await job.messages.next()) ?? { action: "abort", reason: "no reply" };
    };

    let found = await findVideo(page);
    // The click is offered only when the page gave no player by itself, only when the operator
    // opted this site in, and only once. Python decides whether any of these elements may be
    // clicked at all; this side neither knows nor applies that rule.
    let candidates = { descriptions: [], targets: [] };
    if (!found.best && job.allowPlayClick && clicksUsed < 1) {
      candidates = await collectClickCandidates(page, allowedFrame);
    }
    let decision = await report(found, {
      clickCandidates: candidates.descriptions,
      clickPerformed: false,
    });

    if (decision.action === "click") {
      // Belt and braces next to the Python budget: the second click of a visit never happens.
      if (clicksUsed >= 1) {
        return { frames: [], stats, skipReason: "no_video: one click per visit is spent" };
      }
      clicksUsed += 1;
      const target = candidates.targets[decision.index];
      let performed = false;
      if (!target) {
        note(`click target ${decision.index} is not on this page`);
      } else {
        try {
          const handle = await target.frame.evaluateHandle(
            (at) => window.__marcelineClickCandidates[at],
            target.at,
          );
          const element = handle.asElement();
          if (element) {
            await element.click({ timeout: CLICK_TIMEOUT_MS });
            performed = true;
          }
        } catch (error) {
          note(`play click failed: ${error.message}`);
        }
      }
      found = await findVideo(page, AFTER_CLICK_WAIT_MS);
      decision = await report(found, { clickCandidates: [], clickPerformed: performed });
    }

    const best = found.best;
    if (decision.action !== "capture") {
      return { frames: [], stats, skipReason: decision.reason || "aborted" };
    }
    if (!best) return { frames: [], stats, skipReason: "no_video" };
    if (drmSeen) return { frames: [], stats, skipReason: "drm" };

    await startPlayback(best.frame, best.index);
    const video = best.frame.locator("video").nth(best.index);
    // Some players (KVS/flowplayer) keep the <video> hidden until it plays.
    await video.waitFor({ state: "visible", timeout: VISIBLE_WAIT_MS }).catch(() => {});
    const frames = [];
    const sampleStarted = Date.now();
    const budgetMs = job.plan.maxSeconds * 1000;
    for (const position of decision.positions.slice(0, job.plan.maxFrames)) {
      if (Date.now() - sampleStarted > budgetMs) {
        note(`sample budget of ${job.plan.maxSeconds}s reached after ${frames.length} frames`);
        break;
      }
      if (overBudget) {
        note(`byte budget of ${job.plan.maxBytes} reached after ${frames.length} frames`);
        break;
      }
      if (!(await seekTo(best.frame, best.index, position))) continue;
      try {
        // Element screenshot: compositor output, so a cross-origin stream is not a problem and
        // no canvas is tainted. The buffer is handed back and never written to disk.
        const png = await video.screenshot({ type: "png", timeout: SEEK_TIMEOUT_MS });
        frames.push({ position, png: png.toString("base64") });
      } catch (error) {
        note(`screenshot at ${position}s failed: ${error.message}`);
      }
    }
    stats.sampleMs = Date.now() - sampleStarted;
    return { frames, stats, skipReason: frames.length ? "" : "no_frames" };
  } finally {
    stats.wallMs = Date.now() - started;
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }
}

async function main() {
  const messages = readMessages();
  const job = await messages.next();
  if (!job) throw new Error("no job on stdin");
  job.messages = messages;

  const watchdog = new Promise((_, reject) =>
    setTimeout(
      () => reject(new Error(`capture exceeded ${job.plan.timeoutMs} ms`)),
      job.plan.timeoutMs,
    ).unref(),
  );
  const result = await Promise.race([run(job), watchdog]);
  send({ event: "done", ...result });
}

main().catch((error) => {
  send({
    event: "done",
    frames: [],
    stats: { wallMs: 0, sampleMs: 0, bytes: 0, requests: 0, abortedRequests: 0, browser: "" },
    skipReason: `error: ${error.message}`.slice(0, 300),
  });
  process.exitCode = 0;
});
