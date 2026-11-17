// How long does fingerprinting an original in the browser actually take? (issue #29)
//
// The parity harness next door answers "are the hashes good enough"; this one answers "can a
// creator with a cheap laptop or a phone sit through it". It runs the **same** page
// (`page/index.html` → `src/lib/fingerprintClient.ts`, the function the "Add original" panel
// calls) over synthetic clips of several resolutions and lengths, under a given CPU budget, and
// reports seconds of wall time per minute of video.
//
// Usage, from the repo root:
//   make browser-speed
//   make browser-speed SPEED_ARGS="--matrix smoke --keep-clips"
//
// Or directly, from frontend/parity/:
//   node speed.mjs --generate            # write the clips, hash nothing
//   node speed.mjs --matrix full         # generate what is missing, then measure
//   node speed.mjs --matrix hidden --hidden      # the same row with the tab in the background
//
// A slow machine is a **cgroup**, not DevTools' CPU throttling — see PLANS below for why, and for
// the three `systemd-run` invocations that produce the device table.
//
// **Synthetic media only.** Every clip is generated here by ffmpeg (`testsrc2` plus temporal
// noise, so the encoder and the decoder both do realistic work) and deleted afterwards unless
// `--keep-clips` is passed. Never point this at real, customer or pirated media.

import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "vite";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PAGE_DIR = path.join(HERE, "page");
const DIST_DIR = path.join(PAGE_DIR, "dist-speed");
const CLIP_DIR = path.join(HERE, "speed-clips");
const OUT_DIR = path.join(HERE, "out");

/** One clip a whole run is spent on; a 40 min 4K file at 6x can take the better part of an hour. */
const RUN_TIMEOUT_MS = 90 * 60 * 1000;

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".wasm": "application/wasm",
  ".map": "application/json; charset=utf-8",
};

/**
 * The masters. One 60 s encode per shape, looped without re-encoding into the lengths below, so a
 * 40 min file costs one minute of x264 rather than forty.
 *
 * Bitrates are what a real camera or export preset produces at that size, because the decoder's
 * work per frame is what is being measured and a toy bitrate would understate it. The GOP is 2 s
 * (`-g 60` at 30 fps), which is also what real files carry — and it is what a seek costs, since a
 * seek to an arbitrary second decodes from the preceding keyframe forward.
 */
const MASTERS = [
  { name: "720p-h264", width: 1280, height: 720, fps: 30, bitrateK: 5000, codec: "h264" },
  { name: "1080p-h264", width: 1920, height: 1080, fps: 30, bitrateK: 10000, codec: "h264" },
  { name: "2160p-h264", width: 3840, height: 2160, fps: 30, bitrateK: 25000, codec: "h264" },
  { name: "1080p-hevc", width: 1920, height: 1080, fps: 30, bitrateK: 6000, codec: "hevc" },
];

/**
 * The plans, as explicit rows rather than a cross product.
 *
 * **Two different ways to stand in for a slow machine, and only one of them works here.**
 *
 * `throttles` is CDP `Emulation.setCPUThrottlingRate`, which is what DevTools' "4x slower" preset
 * does. Measured on 2026-09-19 it moves this pipeline hardly at all — at 4x a 720p run went from
 * 51.6 s to 71.2 s — because it slows the renderer's **main thread** and nothing else, while this
 * pipeline spends its time in the media decoder and in a Worker. So it is kept (plan `cdp`) as a
 * measurement of the script side, and it is **not** the answer to "what does a cheap laptop do".
 *
 * The answer to that is a CPU budget over the whole browser, which a cgroup gives and CDP cannot.
 * Run the plan under one, from outside:
 *
 *   node speed.mjs --matrix devices --label desktop
 *   sudo systemd-run --scope -p AllowedCPUs=0-1 -p CPUQuota=100% -- node speed.mjs \
 *       --matrix devices --label laptop --out out/speed-laptop.json
 *   sudo systemd-run --scope -p AllowedCPUs=0-1 -p CPUQuota=50% -- node speed.mjs \
 *       --matrix devices --label phone --out out/speed-phone.json
 *
 * Every child — the browser process, the renderer, the media process and the Worker thread — is in
 * that cgroup, so the whole pipeline gets the budget the label names. `--label` is recorded per row
 * so the merged report says which budget produced which number.
 *
 * `full` is deliberately not a cross product: a 40 min 4K row is an hour of a shared host's CPU for
 * a number the 10 min row plus the linearity check already pin down. Rows left out are missing from
 * the report rather than extrapolated into it.
 */
const PLANS = {
  smoke: [{ master: "720p-h264", minutes: 10, throttles: [1] }],
  // What "keep this tab open" is worth, measured instead of assumed: the same row with the tab in
  // the background (`--hidden`), where `requestVideoFrameCallback` does not fire and `setTimeout`
  // is throttled. Run it with and without `--hidden` and compare. One minute of video, because if
  // the browser throttles its timers to one a minute this row would otherwise never finish — and
  // a row that never finishes is the answer, not a bug.
  hidden: [{ master: "720p-h264", minutes: 1, throttles: [1] }],
  // The script side on its own: what DevTools' CPU throttling actually reaches.
  cdp: [
    { master: "720p-h264", minutes: 10, throttles: [1, 4, 6] },
    { master: "1080p-h264", minutes: 10, throttles: [1, 4, 6] },
  ],
  // Run this one once per CPU budget (see above). 1080p at 40 min is the linearity check.
  devices: [
    { master: "720p-h264", minutes: 10, throttles: [1] },
    { master: "1080p-h264", minutes: 10, throttles: [1] },
    { master: "2160p-h264", minutes: 10, throttles: [1] },
    { master: "1080p-h264", minutes: 40, throttles: [1] },
  ],
  // Every shape and both lengths, at the host's full speed: the shape of the cost, and the codec
  // answer (HEVC) that does not depend on how fast the machine is.
  full: [
    { master: "720p-h264", minutes: 10, throttles: [1] },
    { master: "1080p-h264", minutes: 10, throttles: [1] },
    { master: "2160p-h264", minutes: 10, throttles: [1] },
    { master: "1080p-hevc", minutes: 10, throttles: [1] },
    { master: "720p-h264", minutes: 40, throttles: [1] },
    { master: "1080p-h264", minutes: 40, throttles: [1] },
    { master: "2160p-h264", minutes: 40, throttles: [1] },
  ],
};

function parseArgs(argv) {
  const args = {
    matrix: "full",
    // The CPU budget this invocation was given from outside (a cgroup), recorded per row so a
    // merged report can say which budget produced which number. "host" means no budget at all.
    label: "host",
    fps: 1,
    out: path.join(OUT_DIR, "speed.json"),
    keepClips: false,
    generateOnly: false,
    regenerate: false,
    headed: false,
    // Put the measured page in the background before the run, by bringing a second page to the
    // front. `document.visibilityState` becomes "hidden" and the browser's own throttling applies.
    hidden: false,
    seekProbe: 30,
    // The check runner is shared. A neighbour's `make check` runs pytest on every core, which
    // would silently make a throttled row look slower than the throttle it names — so wait for the
    // host to be quiet before each row, and record what the load actually was.
    maxLoad: 3,
    maxWaitMin: 90,
  };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    const value = argv[i + 1];
    if (flag === "--matrix") (args.matrix = value), i++;
    else if (flag === "--fps") (args.fps = Number(value)), i++;
    else if (flag === "--out") (args.out = path.resolve(value)), i++;
    else if (flag === "--seek-probe") (args.seekProbe = Number(value)), i++;
    else if (flag === "--label") (args.label = value), i++;
    else if (flag === "--max-load") (args.maxLoad = Number(value)), i++;
    else if (flag === "--max-wait-min") (args.maxWaitMin = Number(value)), i++;
    else if (flag === "--keep-clips") args.keepClips = true;
    else if (flag === "--generate") ((args.generateOnly = true), (args.keepClips = true));
    else if (flag === "--regenerate") args.regenerate = true;
    else if (flag === "--headed") args.headed = true;
    else if (flag === "--hidden") args.hidden = true;
    else throw new Error(`unknown argument ${flag}`);
  }
  if (PLANS[args.matrix] === undefined) {
    throw new Error(`--matrix must be one of ${Object.keys(PLANS).join(", ")}`);
  }
  if (!Number.isFinite(args.fps) || args.fps <= 0) throw new Error("--fps must be positive");
  return args;
}

function run(command, commandArgs) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, commandArgs, { stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
      if (stderr.length > 200_000) stderr = stderr.slice(-100_000);
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${command} exited ${code}: ${stderr.slice(-2000)}`));
    });
  });
}

async function exists(file) {
  try {
    await stat(file);
    return true;
  } catch {
    return false;
  }
}

/** One 60 s master. Noise makes the encoder spend its bitrate and the decoder earn it. */
async function encodeMaster(master) {
  const file = path.join(CLIP_DIR, `${master.name}.master.mp4`);
  const source = `testsrc2=size=${master.width}x${master.height}:rate=${master.fps}:duration=60`;
  const encoder =
    master.codec === "hevc"
      ? ["-c:v", "libx265", "-preset", "veryfast", "-tag:v", "hvc1", "-x265-params", "log-level=error"]
      : ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high"];
  await run("ffmpeg", [
    "-hide_banner",
    "-loglevel", "error",
    "-y",
    "-f", "lavfi",
    "-i", source,
    "-vf", "noise=alls=12:allf=t+u,format=yuv420p",
    ...encoder,
    "-g", String(master.fps * 2),
    "-keyint_min", String(master.fps * 2),
    "-sc_threshold", "0",
    "-b:v", `${master.bitrateK}k`,
    "-maxrate", `${Math.round(master.bitrateK * 1.2)}k`,
    "-bufsize", `${master.bitrateK * 2}k`,
    "-an",
    "-movflags", "+faststart",
    file,
  ]);
  return file;
}

/** The master, concatenated with itself, stream-copied: a real long file for one minute of encode. */
async function loopMaster(master, masterFile, minutes) {
  const file = path.join(CLIP_DIR, `${master.name}.${minutes}min.mp4`);
  const listFile = path.join(CLIP_DIR, `${master.name}.${minutes}min.txt`);
  const line = `file '${masterFile.replaceAll("'", "'\\''")}'\n`;
  await writeFile(listFile, line.repeat(minutes));
  await run("ffmpeg", [
    "-hide_banner", "-loglevel", "error", "-y",
    "-f", "concat", "-safe", "0", "-i", listFile,
    "-c", "copy", "-movflags", "+faststart",
    file,
  ]);
  await rm(listFile, { force: true });
  return file;
}

/** Every clip the plan's rows name, generated once and reused across throttling rates. */
async function generateClipsForPlan(plan, regenerate) {
  await mkdir(CLIP_DIR, { recursive: true });
  const byName = new Map();
  for (const step of plan) {
    const master = MASTERS.find((entry) => entry.name === step.master);
    if (master === undefined) throw new Error(`no master named ${step.master}`);
    const key = `${step.master}.${step.minutes}`;
    if (byName.has(key)) continue;
    const masterFile = path.join(CLIP_DIR, `${master.name}.master.mp4`);
    if (regenerate || !(await exists(masterFile))) {
      process.stderr.write(`encoding ${master.name} master (60 s, ${master.bitrateK} kbit/s)\n`);
      await encodeMaster(master);
    }
    const file = path.join(CLIP_DIR, `${master.name}.${step.minutes}min.mp4`);
    if (regenerate || !(await exists(file))) {
      process.stderr.write(`looping ${master.name} to ${step.minutes} min\n`);
      await loopMaster(master, masterFile, step.minutes);
    }
    const { size } = await stat(file);
    byName.set(key, { master: master.name, minutes: step.minutes, file, size, ...master });
  }
  return byName;
}

/** `configFile: false` keeps the app's vite.config.ts (CSP, proxy) out, exactly as parity does. */
async function buildPage() {
  await build({
    root: PAGE_DIR,
    base: "./",
    configFile: false,
    logLevel: "warn",
    worker: { format: "es" },
    build: { outDir: DIST_DIR, emptyOutDir: true, target: "es2022", sourcemap: false },
  });
}

async function serve(directory) {
  const server = createServer(async (request, response) => {
    const requested = new URL(request.url, "http://127.0.0.1").pathname;
    const relative = requested === "/" ? "index.html" : requested.replace(/^\/+/, "");
    const file = path.join(directory, relative);
    if (!file.startsWith(directory)) {
      response.writeHead(403).end("forbidden");
      return;
    }
    try {
      const body = await readFile(file);
      response.writeHead(200, {
        "content-type": MIME[path.extname(file)] ?? "application/octet-stream",
      });
      response.end(body);
    } catch {
      response.writeHead(404).end("not found");
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return { url: `http://127.0.0.1:${port}/`, close: () => new Promise((done) => server.close(done)) };
}

/**
 * Resident memory of every Chromium process on this host, summed, in bytes.
 *
 * Playwright's JS API does not hand out the browser's pid, and the renderer is a child of it
 * anyway, so this reads `/proc` and matches on the process name. Nothing else on a check runner
 * runs Chromium; where `/proc` is absent it returns 0 and the report says "not measured".
 */
async function chromiumRssBytes() {
  let total = 0;
  let largest = 0;
  let matched = 0;
  let entries;
  try {
    entries = await readdir("/proc");
  } catch {
    return { bytes: 0, largest: 0, processes: 0 };
  }
  for (const entry of entries) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const comm = (await readFile(`/proc/${entry}/comm`, "utf8")).trim();
      if (!/chrome|headless_shell/i.test(comm)) continue;
      const status = await readFile(`/proc/${entry}/status`, "utf8");
      const rss = /^VmRSS:\s+(\d+) kB$/m.exec(status);
      if (rss === null) continue;
      const bytes = Number(rss[1]) * 1024;
      total += bytes;
      largest = Math.max(largest, bytes);
      matched += 1;
    } catch {
      // The process exited between readdir and read; nothing to account for.
    }
  }
  // The sum double-counts shared pages, so it is an upper bound; `largest` is the renderer or
  // browser process on its own, which is the number a per-tab memory limit is compared against.
  return { bytes: total, largest, processes: matched };
}

function loadAverage() {
  // Read fresh each time: a neighbour's build on the host is the one thing that would silently
  // invalidate every number below, so the report carries the load it ran under.
  return os.loadavg();
}

/** Wait for the shared host to go quiet, so a throttled row measures the throttle and not a queue. */
async function waitForQuiet(maxLoad, maxWaitMin) {
  const deadline = Date.now() + maxWaitMin * 60_000;
  let announced = false;
  for (;;) {
    const load = loadAverage()[0];
    if (load <= maxLoad) return { load, waited: announced };
    if (Date.now() > deadline) {
      process.stderr.write(`  giving up on a quiet host after ${maxWaitMin} min (load ${load.toFixed(2)})\n`);
      return { load, waited: true };
    }
    if (!announced) {
      process.stderr.write(`  waiting for the host to go quiet (load ${load.toFixed(2)} > ${maxLoad})…\n`);
      announced = true;
    }
    await new Promise((resolve) => setTimeout(resolve, 30_000));
  }
}

async function measure(page, cdp, clip, throttle, args) {
  const quiet = await waitForQuiet(args.maxLoad, args.maxWaitMin);
  await page.goto(args.origin, { waitUntil: "load" });
  await page.waitForFunction(() => window.marcelineParity !== undefined);
  await page.setInputFiles("#clip", clip.file);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: throttle });
  let foreground = null;
  if (args.hidden === true) {
    foreground = await page.context().newPage();
    await foreground.goto("about:blank");
    await foreground.bringToFront();
    const visibility = await page.evaluate(() => document.visibilityState);
    process.stderr.write(`  measured page is now ${visibility}\n`);
  }
  let peakRss = 0;
  let peakLargestRss = 0;
  let rssProcesses = 0;
  const sampler = setInterval(() => {
    void chromiumRssBytes().then(({ bytes, largest, processes }) => {
      peakLargestRss = Math.max(peakLargestRss, largest);
      if (bytes > peakRss) {
        peakRss = bytes;
        rssProcesses = processes;
      }
    });
  }, 1000);
  const loadBefore = loadAverage()[0];
  let timed;
  try {
    timed = await page.evaluate((fps) => window.marcelineParity.runTimed(fps), args.fps);
  } finally {
    clearInterval(sampler);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 1 });
    if (foreground !== null) {
      await foreground.close();
      await page.bringToFront();
    }
  }
  // The seek probe runs unthrottled and only on the shortest clip of each shape: it answers
  // "seek or hash", which does not need the whole matrix, and it costs a second run otherwise.
  let seek = null;
  if (args.seekProbe > 0 && throttle === 1 && timed.ok) {
    // A probe that cannot open the file is a fact about the codec, already recorded by the run
    // above; it must not end a plan that still has rows to measure.
    try {
      seek = await page.evaluate(
        ([fps, positions]) => window.marcelineParity.probeSeek(fps, positions),
        [args.fps, args.seekProbe],
      );
    } catch (error) {
      process.stderr.write(`  seek probe skipped: ${error.message.split("\n")[0]}\n`);
    }
  }
  return {
    label: args.label,
    availableCpus: os.availableParallelism?.() ?? os.cpus().length,
    clip: clip.master,
    minutes: clip.minutes,
    codec: clip.codec,
    nativeWidth: clip.width,
    nativeHeight: clip.height,
    sourceFps: clip.fps,
    bitrateK: clip.bitrateK,
    fileBytes: clip.size,
    throttle,
    fps: args.fps,
    seekProbe: seek,
    waitedForQuiet: quiet.waited,
    hidden: args.hidden === true,
    peakChromiumRssBytes: peakRss,
    peakLargestProcessRssBytes: peakLargestRss,
    chromiumProcesses: rssProcesses,
    hostLoad1Before: loadBefore,
    hostLoad1After: loadAverage()[0],
    ...timed,
  };
}

function secondsPerVideoMinute(row) {
  if (!(row.durationS > 0)) return null;
  return (row.elapsedMs / 1000) / (row.durationS / 60);
}

function markdown(document) {
  const lines = [];
  lines.push("# Browser fingerprinting speed", "");
  lines.push(`Generated ${document.generated_at} on ${document.platform}, node ${document.node}.`);
  lines.push(`Browser: ${document.browser} ${document.browser_version}.`);
  lines.push(`Host: ${document.host_cpus} logical CPUs, CPU budget \`${document.label}\`, matrix \`${document.matrix}\`, ${document.fps} fps.`);
  lines.push("");
  lines.push("`s/min` is seconds of wall time per minute of video — the number a creator feels.");
  lines.push("");
  lines.push(
    "| budget | clip | codec | length | file MB | tab | CDP | positions | wall s | **s/min** | frame s | hash s | digest s | peak heap MB | peak proc RSS MB | all Chromium RSS MB |",
  );
  lines.push("|---|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|");
  for (const row of document.rows) {
    const head = `| ${row.label} | ${row.clip} | ${row.codec} | ${row.minutes} min | ${(row.fileBytes / 1048576).toFixed(0)} | ${row.hidden ? "hidden" : "visible"} | ${row.throttle}x`;
    if (!row.ok) {
      lines.push(`${head} | — | — | — | — | — | — | — | — | — |`);
      continue;
    }
    const perMinute = secondsPerVideoMinute(row);
    lines.push(
      `${head} | ${row.keptPositions}/${row.positions} | ${(row.elapsedMs / 1000).toFixed(1)} | **${perMinute === null ? "—" : perMinute.toFixed(1)}** | ${(row.frameMs / 1000).toFixed(1)} | ${(row.hashMs / 1000).toFixed(1)} | ${(row.digestMs / 1000).toFixed(1)} | ${(row.peakHeapBytes / 1048576).toFixed(0)} | ${(row.peakLargestProcessRssBytes / 1048576).toFixed(0)} | ${(row.peakChromiumRssBytes / 1048576).toFixed(0)} |`,
    );
  }
  lines.push("");
  const failures = document.rows.filter((row) => !row.ok);
  if (failures.length > 0) {
    lines.push("## Refused", "");
    for (const row of failures) {
      lines.push(`- ${row.clip} ${row.minutes} min at ${row.throttle}x: ${row.error?.name}: ${row.error?.message}`);
    }
    lines.push("");
  }
  const probes = document.rows.filter((row) => row.seekProbe !== null && row.ok);
  if (probes.length > 0) {
    lines.push("## Seek alone (unthrottled, `seekTo` with no canvas and no hash)", "");
    lines.push("| clip | length | seeks | ms per seek |", "|---|---:|---:|---:|");
    for (const row of probes) {
      lines.push(
        `| ${row.clip} | ${row.minutes} min | ${row.seekProbe.seeks} | ${(row.seekProbe.seekMs / row.seekProbe.seeks).toFixed(0)} |`,
      );
    }
    lines.push("");
  }
  const canPlay = document.rows.find((row) => row.canPlay !== undefined)?.canPlay;
  if (canPlay !== undefined) {
    lines.push("## `canPlayType` in this browser", "");
    lines.push(
      Object.entries(canPlay)
        .map(([codec, answer]) => `- ${codec}: ${answer}`)
        .join("\n"),
    );
    lines.push("");
  }
  lines.push("## Host load while measuring", "");
  lines.push("A neighbour's build on the same host is the one thing that invalidates every number above.");
  lines.push("");
  lines.push("| clip | length | throttle | load before | load after |", "|---|---:|---:|---:|---:|");
  for (const row of document.rows) {
    lines.push(
      `| ${row.clip} | ${row.minutes} min | ${row.throttle}x | ${row.hostLoad1Before.toFixed(2)} | ${row.hostLoad1After.toFixed(2)} |`,
    );
  }
  return `${lines.join("\n")}\n`;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const plan = PLANS[args.matrix];

  const clips = await generateClipsForPlan(plan, args.regenerate);
  for (const clip of clips.values()) {
    process.stderr.write(`clip ${clip.master} ${clip.minutes} min: ${(clip.size / 1048576).toFixed(0)} MB\n`);
  }
  if (args.generateOnly) return;

  process.stderr.write("building the harness page (vite)\n");
  await buildPage();
  const server = await serve(DIST_DIR);
  args.origin = server.url;
  process.stderr.write(`serving ${DIST_DIR} on ${server.url}\n`);

  const playwright = await import("playwright");
  const browser = await playwright.chromium.launch({ headless: !args.headed });
  const browserVersion = browser.version();
  const rows = [];
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    page.setDefaultTimeout(RUN_TIMEOUT_MS);
    const cdp = await context.newCDPSession(page);
    for (const step of plan) {
      const clip = clips.get(`${step.master}.${step.minutes}`);
      for (const throttle of step.throttles) {
        process.stderr.write(`measuring ${clip.master} ${clip.minutes} min at ${throttle}x…\n`);
        const row = await measure(page, cdp, clip, throttle, args);
        const perMinute = secondsPerVideoMinute(row);
        process.stderr.write(
          row.ok
            ? `  ${(row.elapsedMs / 1000).toFixed(1)} s wall, ${perMinute === null ? "?" : perMinute.toFixed(1)} s per video minute\n`
            : `  FAILED ${row.error?.name}: ${row.error?.message}\n`,
        );
        rows.push(row);
        // Written after every row: a plan of seven rows is hours, and a crash in the last one must
        // not throw away the first six.
        await writeReport({ args, browserVersion, rows });
      }
    }
    await context.close();
  } finally {
    await browser.close();
    await server.close();
  }
  await writeReport({ args, browserVersion, rows });
  if (!args.keepClips) {
    await rm(CLIP_DIR, { recursive: true, force: true });
    process.stderr.write(`removed ${CLIP_DIR}\n`);
  }
}

async function writeReport({ args, browserVersion, rows }) {
  const document = {
    generated_at: new Date().toISOString(),
    node: process.version,
    platform: `${process.platform} ${process.arch}`,
    host_cpus: os.cpus().length,
    browser: "chromium",
    browser_version: browserVersion,
    label: args.label,
    matrix: args.matrix,
    fps: args.fps,
    rows,
  };
  await mkdir(path.dirname(args.out), { recursive: true });
  await writeFile(args.out, `${JSON.stringify(document, null, 2)}\n`);
  const reportPath = args.out.replace(/\.json$/, ".md");
  await writeFile(reportPath, markdown(document));
  process.stderr.write(`wrote ${args.out} and ${reportPath}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack ?? error}\n`);
  process.exit(1);
});
