# 0044 — Cloud fingerprinting: the customer uploads, we hash, the bytes are deleted

Status: **proposed** (2026-09-18) — built behind `CLOUD_FINGERPRINTING_ENABLED`, which is **off**.
What may be switched on is the **narrowed** feature of the amendment of
2026-09-19, and only after the ordered preconditions in that amendment's § C. Issue #59.
**Read §1 and §9 through that amendment**: the streaming relay is built, so the routes are no longer
development-only when the public host runs in relay mode. Builds on ADR 0010 (references),
ADR 0017/0019 (browser fingerprinting), ADR 0020/0022 (photo collections, pipeline tags),
ADR 0021/0031 (tenancy and ownership), ADR 0028 (production is a read-through cache),
ADR 0034 (rights), ADR 0036 (the Next.js customer app). Replaces the recommendation of
`docs/research/cloud-fingerprinting.md` on where the bytes go.

## Context

A customer registers an original by fingerprinting it **on their own machine**: the browser client
(ADR 0017/0019, `frontend/src/lib/fingerprintClient.ts`) decodes the video with the platform
decoder, hashes every sampled moment with Meta's PDQ compiled to WebAssembly, and posts a
hashes-only document to `POST /api/v1/app/fingerprints`. The video never leaves the machine. That
is the product's strongest privacy statement and it stays the default.

It is also, in the operator's words of 2026-09-18, **very compute-heavy for customers**. Measured
consequences we already count (`matching.ClientEvent`, `codec_unsupported` / `client_failed`,
issue #29): a browser cannot decode ProRes or DNxHD masters at all, Firefox will not decode HEVC on
most installs, and a laptop hashing a 45-minute 4K file runs its fan for half an hour. A studio with
a back catalogue of several hundred files cannot do it at all.

The alternative the operator asked for: **the customer uploads the file, we fingerprint it for
them, securely and without data retention.** That is a different product in three respects.

1. **We receive the media.** Unreleased videos of identifiable people — sensitive personal data
   about the customer *and* about co-performers who are not our customers. Even for
   minutes, this is the highest-impact breach scenario the product can have, and it changes the
   sentence the marketing site relies on ("the video never
   leaves your machine"). The wording changes with the feature, not after it.
2. **We decode untrusted media.** ffmpeg and Pillow on a file an account holder chose is the
   largest attack surface this project has ever had. Every other decode in this system runs on
   bytes the *crawler* fetched, which is also untrusted — but those are ≤ 32 MB previews from an
   allowlist of sites, not a 10 GB container an attacker composes deliberately.
3. **It may make us a hosting service.** Accepting user content for storage, however briefly, is
   the definition the Digital Services Act works from, which is one more reason the flag is off.

What it *buys*, beyond convenience: the server pipeline can produce things the browser structurally
cannot — the master frame (and therefore ADR 0039's reference-side crop grid), the embedding
sketches of ADR 0037, and hashes that are bit-identical with the operator's own CLI references
rather than a separate `browser-…` pipeline tag that is measurably a few bits off (ADR 0017).

Constraints that decide most of the design:

- **Production is a 2 vCPU / 4 GB public cache** (ADR 0028). It holds no originals, its web image
  has **no ffmpeg**, and its only credential towards development is a scoped, read-only export
  token. The project's own rule for the one sensitive upload that already exists — rights proof
  documents, ADR 0034 — is that they stay on development and are **404 on production**, "not even
  in transit through an outbox".
- **Development has the CPU, the code and ffmpeg**, and is reachable over Tailscale only. Measured
  on the development VPS on 2026-09-18 with a synthetic 1920×1080 / 30 fps / 60 s H.264 clip
  through the real `matching.fingerprint.fingerprint_video` at 1 fps: **7.8 s wall, 12.9 CPU-s,
  167 MB peak RSS** (44 MB parent + 123 MB ffmpeg). That is **≈ 13 CPU-minutes per video-hour** —
  the low-middle of the 5–30 estimate in `docs/research/cloud-fingerprinting.md`, now measured. The
  box has 4 cores, 7 GB of RAM (3 GB free) and 198 GB of free disk.
- **Hashes must be the ones everything else produces.** A cloud-fingerprinted original that is not
  bit-comparable with the rest of the library is worse than no feature.

## Decision

### 1. The bytes never touch the public host. Uploads are development-only in v1

`POST /api/v1/app/uploads…` joins `ROLE_ONLY_PREFIXES["dev"]` in `marceline/public_surface.py`,
beside `/api/v1/app/rights/proofs` and `/api/v1/app/notices`. On `MARCELINE_ROLE=prod` every upload
route answers the same bare 404 as an unknown path, and `me.can_upload_media` is false there.

The argument is the project's own, applied consistently: if a contract excerpt may not be stored on
the public host "not even in transit", a 4 GB unreleased video certainly may not. The options were:

| Option | Bytes at rest on the public host | Production's credentials | Verdict |
|---|---|---|---|
| Upload to production, spool there, hash there | yes, gigabytes | unchanged | **rejected**: 40 GB disk, no ffmpeg in the image, 4 GB RAM shared with Postgres, Caddy, gunicorn and Next.js — and it puts customer media on the internet-facing machine, which is the one thing ADR 0028 was written to avoid |
| Production accepts chunks into the outbox, development pulls | yes, gigabytes, in Postgres | unchanged | **rejected twice over**: the outbox is a database table (ADR 0028 caps a batch at 8 MiB), and media in Postgres breaks the hardest rule in `CLAUDE.md` |
| Production as a pure **streaming relay** — each ≤ 8 MiB chunk forwarded to development over Tailscale in memory, nothing written | no | **new**: production would need an append-capable credential towards development | **designed below, not built** (§9): it is the right answer for the public host, and it is the one change that widens production's credentials |
| Uploads on development only, over Tailscale | no | unchanged | **chosen for v1** |
| A separate ingest host | no (its own) | unchanged | **rejected for now**: new hardware, new attack surface, new backup and update duty, for a feature that is switched off |

What this costs: with the flag on, a customer reaching `dash.marceline.studio` cannot upload; the
operator can, over Tailscale, with the test customer and the demo dataset, which is exactly the
exercise the flag is for. What it saves: **no new credential, no new byte path, no change to
`infra/prod/*` beyond making the refusal explicit and tested.** The job result needs no transport at
all — the `Reference` is created on development, where it already lives, and reaches the customer
through the signed bundle and the purge that ADR 0028 already runs.

### 2. Chunked upload over plain HTTP, assembled server-side. No new protocol

Three routes, all under `/api/v1/app/uploads`, all CSRF-checked, all session-authenticated, all
scoped by organisation with the 404 rule:

| Route | Body | What |
|---|---|---|
| `POST /api/v1/app/uploads` | JSON, ≤ 1 MB | declare one upload: `file_size`, `title`, `creators`, `fps`, and the rights declaration. Answers `upl_…`, `chunk_bytes` and how many chunks. Everything that can be refused without the file is refused **here** |
| `POST /api/v1/app/uploads/{id}/chunks/{n}` | raw bytes, ≤ `CHUNK_BYTES + 1024` | one fixed-size chunk (the last is short). Re-sending chunk *n* overwrites it, so a retry is free and a resume is `received_bytes // chunk_bytes` |
| `POST /api/v1/app/uploads/{id}/complete` | JSON, ≤ 1 MB | the customer asserts the file's sha256; the server compares it with what it assembled and queues the job |

plus `GET /api/v1/app/uploads` and `GET …/{id}` (status) and `DELETE …/{id}` (cancel: the bytes go
immediately).

Why not tus, and why not S3 multipart — both of which `docs/research/cloud-fingerprinting.md`
preferred: tus is a second server (a Go binary, its own container, its own hook protocol, its own
update duty) for a resume semantics we get in nine lines from a fixed chunk size and a byte count;
S3 multipart is object storage we do not have, an SSE-C key scheme, a bucket lifecycle rule and a
second copy of the tenancy check. Both were designed for uploads that must survive weeks and
thousands of concurrent users. We have one machine, a handful of customers and an opt-in feature
that is off.

Numbers, all settings, all refusable:

- `CLOUD_UPLOAD_CHUNK_BYTES` = **8 MiB**. Small enough that a chunk is a short request even on a
  slow uplink and that a gunicorn worker is never tied up for minutes (the failure mode the research
  doc rejected "through Django" for); large enough that a 4 GB file is 500 requests, not 40,000.
- `CLOUD_UPLOAD_MAX_BYTES` = **10 GiB** per file, the research doc's number.
- `CLOUD_UPLOAD_SPOOL_MAX_BYTES` = **40 GiB** for the whole spool. A new upload is refused
  (`spool_full`, 503) rather than filling a disk that Postgres lives on.
- `CLOUD_UPLOAD_DAILY_BYTES_PER_ORG` = **60 GiB** per rolling 24 h, and the existing
  `marceline/app/upload_throttle.py` counts upload *creations* under a new kind. Two limits, because
  one bounds bytes and the other bounds jobs.
- **Concurrency is one.** A single `fingerprint_worker` process holds a Postgres advisory lock,
  claims one queued job at a time with `SKIP LOCKED` and hashes it. At 13 CPU-minutes per
  video-hour, one core is the honest budget on a box whose other three cores crawl; a second worker
  would fight the crawl runtime for RAM. A busy box simply means a job waits, and the status says
  `queued`, with no promise of when (the wording tests ban one — §7).

### 3. "Without data retention", made enforceable

The spool is `CLOUD_UPLOAD_SPOOL_DIR`, an absolute path outside the repository, mode 0700, in its
own Docker volume (`upload-spool`) mounted into exactly two services. Each job gets a directory
named after a random token — never anything the customer sent.

| Guarantee | How it is enforced | The test that fails without it |
|---|---|---|
| (a) **Outside every backup** | `infra/backup/*` push **one Postgres dump** and one curated `~/.claude` tarball, nothing else, and a named Docker volume is in neither | `test_no_retention.py::test_backup_scripts_never_name_the_spool` greps `infra/backup/` for the volume and the setting |
| (b) **Never in Postgres** | no model has a file, blob or path field; the job row stores digests, sizes, timestamps and codes | `test_no_retention.py::test_no_model_field_holds_bytes_or_a_path` walks `MediaUpload`'s fields |
| (c) **Never in a log or a Sentry event** | the spool path never reaches a log line (jobs are logged by `upl_…`); `core/sentry.py` already drops request bodies, cookies, headers and local variables, and `scrub_urls` cleans exception text | `test_no_retention.py::test_nothing_logs_the_spool_path` runs a failing job with `assertLogs` |
| (d) **Deleted on every exit** | `service.run_job` deletes the job directory in a `finally`, on success, on refusal, on a decoder crash and on a timeout | four tests, one per exit |
| (e) **Deleted after a crash** | `spool.reap(ttl)` removes every job directory older than `CLOUD_UPLOAD_TTL_HOURS` (**6**) whatever the database says, and marks its row `expired`. It runs at the top of every worker poll **and** as a scheduler job, so a dead worker is not the only thing that could delete a file | `test_no_retention.py::test_the_reaper_removes_a_directory_no_row_knows_about` |
| (f) **Provably gone** | the row carries `bytes_deleted_at`, set only after `rmtree` succeeded **and** a follow-up `exists()` is false. Nothing else writes it | `test_no_retention.py::test_deleted_at_is_only_set_after_the_directory_is_really_gone` |

**The customer's file name is never transmitted and never stored.** The browser's file picker gives
one; the UI uses it to *pre-fill the title field*, which the customer can edit, and only the title
is sent — exactly the field `add_reference` and the browser client already carry. A file name can
carry a performer's legal name, a client's name or a shoot date; a title the customer chose is the
customer's own statement. There is therefore nothing to hash and nothing to redact.

**Encryption at rest is the host's job, and the ADR says so plainly rather than pretending.** The
spool is a plain file on the volume while it is being hashed. Per-file crypto-erase (a per-upload
key, chunks encrypted on arrival) was designed and rejected: ffmpeg needs a **seekable** file for
every container whose index sits at the end — which is most camera and editor output — so the worker
would have to write the plaintext to the same disk anyway, and the only thing the scheme would buy
is a longer file to delete. What actually protects the bytes against disk theft and
decommissioning is **full-disk or volume-level encryption under the Docker volume**, which is an
operator step in `docs/runbooks/production.md`.

### 4. Decoding untrusted media: the containment

Nothing about ffmpeg's record allows us to treat a customer upload as friendly input. Four walls,
outermost first:

1. **The container.** `fingerprint-worker` in `infra/compose.yaml` (profile `uploads`, never started
   by `make up` or `make workers`) runs on an **internal** Docker network that reaches Postgres and
   nothing else — no route to the internet at all. `read_only: true` root filesystem,
   `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `pids_limit`, `mem_limit: 2g`,
   `cpus: "1.0"`, a small tmpfs for `/tmp`, and exactly one writable mount: the spool.
2. **The child process.** The decode does **not** run in the worker. `matching/cloud_decoder.py` is
   a `python -m` entry point that reads a JSON spec on stdin and writes a fingerprint document on
   stdout. It **imports no Django**, and the parent spawns it with a **scrubbed environment** — no
   `POSTGRES_*`, no `DJANGO_SECRET_KEY`, no key path, nothing but `PATH`, `LANG`,
   `PYTHONDONTWRITEBYTECODE` and the few ffmpeg locale variables. A test asserts the exact
   environment. Its `preexec_fn` applies `RLIMIT_AS` (1.5 GiB, below the container's limit so a
   runaway file fails one job instead of having the kernel kill the worker), `RLIMIT_CPU`,
   `RLIMIT_FSIZE` (**0** —
   the decoder may not create a file, which is also why the byte-code write is disabled) and
   `RLIMIT_NOFILE`, and the parent kills the whole process group on `CLOUD_DECODE_TIMEOUT_S`.
3. **ffprobe before ffmpeg, as an allowlist.** `matching/cloud_hash.probe()` runs one ffprobe with
   `-protocol_whitelist file` and refuses anything whose **container format**, **video codec**,
   **pixel count**, **duration** or **stream count** is outside a named constant. A refusal here
   costs one probe and never starts a decoder. The allowlists are deliberately short and hold what
   a customer master actually is: `mov,mp4,m4a,3gp,3g2,mj2`, `matroska,webm`, `avi`, `mpegts`; and
   `h264`, `hevc`, `vp8`, `vp9`, `av1`, `mpeg4`, `prores`, `dnxhd`, `mjpeg`. ProRes and DNxHD are on
   it precisely because they are the masters a browser cannot open — they are the feature's reason.
4. **The argv, asserted to the character.** Both the probe and the decode are built by pure
   functions whose output a test compares against the exact list. `-nostdin` and
   `-protocol_whitelist file` are the two that matter: a crafted playlist (`concat`, `.m3u8`, an
   `ffconcat` file listing `http://…` or `/etc/passwd`) is how ffmpeg is turned into a fetcher or a
   file reader, and the whitelist is the only thing that stops it. `matching/frames.py` already
   passes both on every path; this ADR makes changing them a test failure rather than a review
   comment. **Never add a protocol to that list**, and never pass a customer-supplied string into
   an ffmpeg argument.

Images: v1 accepts **video only** (§6), so Pillow is only ever handed frames ffmpeg produced, at a
size the pixel cap already bounds. Decompression bombs therefore arrive, if at all, as a video with
an absurd resolution — which the pixel cap refuses before a decoder starts.

### 5. The same fingerprints as everywhere else

The worker calls `matching.fingerprint.fingerprint_video` — the function `add_reference` and
`client/fingerprint.py` call — with the same defaults, and gets `PIPELINE`
(`ffmpeg-rgb512-pdq-v1`). **Not a new pipeline tag**: it is not a new recipe, it is the same
recipe on a different machine, exactly as `add_reference` on the server and the CLI on the
operator's Mac already share one. `docs/adr/0022` is satisfied by that reading, and a test proves
it: the test generates a tiny synthetic clip with ffmpeg at test time, runs it through
`fingerprint_video` and through the cloud path, and asserts the documents are **equal** — every
hash, every quality, every timestamp.

Where it lands is the existing path, not a copy of it. `marceline/app/fingerprints.py` is
refactored so that everything after `validated()` is one function, `register_fingerprint`, and the
three callers share it: the browser endpoint, the outbox handler, and the upload worker. So a cloud
upload produces exactly the rows a browser submission produces — `import_fingerprint`,
`ownership.register(via=CUSTOMER)`, `rights.services.declare_at_upload`, `RightsConfirmation`,
`marceline.OriginalPublicId` — and is refused by exactly the same rules: the file another
organisation owns, the ≥ 50 % hash overlap, the generic 409 `claim_needs_review` that names nobody
and the staff-only `RefusedClaim` behind it.

**Crop grid and embedding sketches are not produced in v1**, and the reason is arithmetic. ADR
0039's lattice is 256 crops per sampled moment, each scaled by Pillow and PDQ-hashed: a 44-minute
original is 2,640 moments, so 676,000 hashes where the plain recipe computes 21,120 — two orders of
magnitude on a box that has one core to spare. ADR 0037's sketches need the pinned ONNX encoder
(45 MB, fetched per machine, `fetch_embedding_model`) inside a container that deliberately has **no
network**. Both signals are off by default anyway. What this ADR does is leave the door open
honestly: the job row stores the recipe it ran, a gridded reference imports *alongside* a plain one
(ADR 0022), and adding a second job kind later changes no interface. It is the strongest argument
for cloud fingerprinting and it is deferred, not denied.

### 6. Scope: one video per upload

A photo collection (ADR 0020) is N files and one digest; a chunked upload is one byte stream. v1
accepts **one video file**, `kind=video`, `fps` in the existing `SUPPORTED_FPS` ({1.0, 2.0}), at
most `MAX_POSITIONS` (10,800) sampled positions — the same caps the browser endpoint applies. A
collection would need a manifest, a per-file digest and a partial-failure story; it is a second
ADR, not a flag.

### 7. UX and wording

- **Opt-in per upload, never a mode.** `/app/originals/new` keeps local hashing as the visible
  default and the pre-selected choice. The upload alternative appears only when
  `me.can_upload_media` is true, as a second, clearly labelled option with its own sentence about
  what happens to the file. Choosing it is a per-upload act; there is no account setting that
  silently starts sending videos.
- **The sentence is a fact, in both languages.** "Die Datei wird nach der Berechnung gelöscht; wir
  behalten nur die Fingerabdrücke." / "The file is deleted after the fingerprints are computed; we
  keep only the fingerprints." No duration promise: `frontend/src/i18n/wording.test.ts` bans
  `innerhalb (von )?\d+ (Stunden?|Tagen?)` and `within \d+ (hours?|days?)`, `garant`/`guarantee`,
  `erwarte`/`expect` and `wahrscheinlich`/`likely` — and those bans are right here. The status is
  `queued` / `uploading` / `hashing` / `added` / `not added`, never "in about an hour".
- **Progress is bytes, not time.** The island counts chunks it has acknowledged. A reload resumes
  from `received_bytes`. (Amended 2026-09-18, below: after the upload it also shows the
  fingerprinting itself, as sampled moments — still a count, still never a time.)
- **Built in the Next.js customer app only.** `frontend/customer/lib/routes.test.ts` requires every
  `/app` route the Vite SPA still serves to exist in the Next app — not the other way round — so a
  screen that exists only in Next is already allowed, and the ADR 0036 cut-over is the direction of
  travel. The SPA gets no upload screen and needs no exemption.

### 8. Content safety: what the pipeline does, and what it does not

- **No person ever sees the file.** There is no download endpoint, no preview, no admin file field,
  no passthrough (the review UI's image passthrough is for indexed pages and cannot address a
  spool). The worker reads it, hashes it and deletes it.
- **No frame leaves the worker**, ever. The decoder writes one JSON document of hashes to stdout
  and nothing else; `RLIMIT_FSIZE = 0` means it *cannot* write a picture even if someone made it try.
- **We classify nothing.** No age estimation, no content classification, no face detection, no biometric
  processing of any kind — the same structural rule ADR 0037 states for the embedding encoder,
  applied here with more force, because here the subject is the customer's own material. We do not
  run the safety terms over an upload either: those terms judge a *pirate site's text* (ADR 0018,
  `docs/safety.md`), and there is no text here, only pixels we refuse to look at.
- **The pipeline does not judge an upload's content.** What we hold afterwards is what we
  hold for every original: PDQ hashes, a sha256, a duration, a title and a cast the customer typed,
  plus a record of who uploaded it and when.

### 9. What production would need, designed and not built

When the operator wants this on `dash.marceline.studio`, the smallest
change that keeps media off the public host's disk is a **streaming relay**: Caddy raises the body
cap for exactly `POST /api/v1/app/uploads/*/chunks/*`, production's `web` forwards each chunk to
development's Tailscale-only listener as it reads it, holding at most one chunk in memory and
writing nothing, and answers what development answered. It needs one new thing — a scoped
credential that lets production **append to an upload it created**, and nothing else — which is a
real widening of ADR 0028's "read-only export token" and therefore a decision, not a patch. Until
then the routes are 404 there and `make prod-config` keeps
`CLOUD_FINGERPRINTING_ENABLED: "false"` explicit in `infra/prod/compose.yaml` so nobody enables it
by editing an `.env`.

### 10. The flag

`CLOUD_FINGERPRINTING_ENABLED` defaults to false in `settings.py`, is named explicitly in
`infra/compose.yaml` for `web` and `fingerprint-worker` and in `infra/prod/compose.yaml` as
`"false"` (a setting a compose file does not name is silently empty inside the container for ever —
`marceline/tests/test_compose_env.py` exists because of that bug). With the flag off **every upload
route answers 404**, exactly as if the feature were not deployed, and `me.can_upload_media` is
false. Switching it on for a real customer is a separate operator decision; the operator may
exercise it on development with `seed_test_customer` at any time.

## Amendment 2026-09-18 — the customer sees what happens after the upload

§7 above said "progress is bytes, not time" and stopped there: the island counted the chunks it had
sent and then showed one line, "queued". Everything after that — the wait for the worker, the
minutes of hashing, the original appearing, or the file being refused — was invisible, while the
*local* path has shown a digest bar and a frames bar since issue #29. The operator's decision is
that the cloud path has to feel equally honest. Four things change, and nothing about the
containment does.

1. **Progress crosses the boundary as two integers.** The contained child still holds the file and
   **no database credential**, and the parent still never opens the file. The child writes JSON
   lines on the one pipe it already has — `{"progress": {"frames": n, "total": m|null}}` while it
   hashes, then its answer document — and `cloud_hash.parse_progress` reduces each line to two
   integers, coerced on arrival. A line that cannot be reduced to two integers is dropped. So the
   only things that come back from the process holding the customer's video are the hashes and a
   pair of counters, and there is no route by which a file name, a path or a decoder's complaint
   about the bytes could reach a row or a log. The child's **stderr is not even read any more**
   (`DEVNULL`): a decoder's error text can quote what it choked on. `hash_file` therefore drives
   the child with `Popen` and its own deadline rather than `subprocess.run`, and kills the whole
   process group on a timeout — which also reaps ffmpeg, which the old path did not.
2. **The parent writes it, throttled.** `MediaUpload.frames_done` / `frames_total` (migration
   `marceline/0018`) are written by `uploads/service.ProgressWriter`: at most one update a second,
   and only when a whole percent or the denominator changed. A 45-minute original at 1 fps reports
   2,700 times and costs a handful of `UPDATE`s. `frames_total` is **NULL until the probe has read
   a duration** — a null total is "not known yet", and the screen shows an indeterminate bar
   rather than a denominator nobody has. It is an estimate from the declared duration even then,
   so the real count can exceed it and the bar clamps at 100 %.
3. **`GET /api/v1/app/uploads/{id}` is the status the screen polls**, with the list endpoint
   carrying the same fields. Beside the bytes it now answers `frames_done`/`frames_total`,
   `bytes_deleted` and `reason` — a **closed set** the app has a sentence for in both languages:
   `unsupported_format`, `too_long`, `too_large`, `decode_failed`, `timed_out`, `quota`,
   `incomplete`, `needs_review`, `cancelled`, `internal`. `MediaUpload.result_code` is mapped onto
   it and anything unmapped reads `internal`, so a code added anywhere can never reach a customer
   as an unworded English identifier. No file name, no path, no decoder text: a test pins the
   answer's whole key set. `bytes_deleted` is `bytes_deleted_at is not None` — the flag that
   *entitles* the screen to say the file has been deleted, and the only one.
4. **The screen polls, and the overview remembers.** After the last chunk the island asks again
   with a backoff (1 s, then slower, capped), and stops on a terminal state, when the tab is
   hidden, and when it is unmounted — resuming on visibility. The decisions are pure functions in
   `@shared/app/uploadStatus`, so they are tested without a browser or a timer. The customer is
   told they may close the page, which makes the list of open uploads on the overview and on the
   add page part of the feature rather than a nicety: their own uploads, still running or recently
   not added, and nothing else.

What does not change: no duration is promised anywhere (the wording tests ban one and this is the
screen that would want it), nothing is classified, no frame leaves the worker, `RLIMIT_FSIZE`
stays 0, and the routes stay development-only and off by default.

## Amendment 2026-09-18 — first end-to-end trial: proven, and four defects fixed

Everything above had run against fixtures and synthetic clips in tests, but never in a real
container on the development VPS. A supervised trial did exactly that: the flag on for one deploy
only (never in `.env`), the `uploads` profile started, synthetic clips only, everything back to
off afterwards.

**What it proved.** Hash parity: 240/240 frame rows identical to the ordinary local path over the
same file — same PDQ hex, quality, timestamp, dihedral — across two different ffmpeg builds
(container vs. host). Cost: 5.3 CPU-s for a 30 s clip, container cgroup peak 273 MB. All six
retention guarantees of §3 held on the real system: spool empty after every exit; every settled
row's `bytes_deleted_at` matched a verified deletion; the row held digests, sizes, timestamps and
codes only; nothing in `web` or the worker's logs named a file or the spool path; the reap
backstop took an abandoned job at its TTL; the backup scripts named neither the volume nor the
setting. Containment, from `docker inspect` and from inside the container: only the internal
`uploads` network (no default route, DNS fails, outbound connects give `ENETUNREACH`), read-only
root, `cap_drop ALL`, `no-new-privileges`, and the process tree was exactly worker → `python -m
matching.cloud_decoder` → `ffmpeg -nostdin … -protocol_whitelist file`.

**What it found, and what was fixed for each.**

1. **The container-path guard called every absolute path "inside the repository".**
   `marceline/uploads/spool.py` tested the repository root — `BASE_DIR`'s parent — as an ancestor
   of the spool path. In the container `backend/` is bind-mounted at `/app`, so that root is `/`,
   and an ancestor test against `/` is true for everything: with the flag on, `spool.available()`
   was false, every `/api/v1/app/uploads` route answered 404 and `fingerprint_worker` refused to
   start. Fixed on branch `cloud-spool-container-path` (the trial ran with the fix): the
   repository is now only consulted when it is a real directory above the code, extracted as the
   shared `core.checkout_paths.inside_checkout` so both callers below apply one rule.
2. **The identical bug was in the rights proof vault.** `marceline/rights/vault.py` had the same
   ancestor test, so `RIGHTS_VAULT_DIR` was refused in a container for the same wrong reason. Now
   delegates to `core.checkout_paths.inside_checkout` too.
3. **The progress bar never reached its total.** `uploads/service.ProgressWriter`'s one-write-a-
   second throttle could drop the decode's own last update, so a finished job settled on a stale
   count (23 of 30 sampled moments). `ProgressWriter.flush()` writes the final count
   unconditionally; `run_job` calls it once, with the fingerprint's own frame count, after
   `hash_file` returns.
4. **The spool root stayed at the volume mount point's default mode, 0755, not the 0700 this ADR
   and the module's docstring promise.** `mkdir(exist_ok=True, mode=0o700)` does not change an
   existing directory's mode. `spool.problems()` now chmods the root back when the process owns
   it, and reports it — rather than silently leaving a 0755 root — when it does not. Per-job
   directories and files were already correct, which is why the trial did not notice until the
   root's own mode was checked directly.

Two smaller things, not defects in the machinery: `infra/compose.yaml` passed
`CLOUD_UPLOAD_CHUNK_BYTES` and its siblings to `web`/`fingerprint-worker` as `${VAR:-}` — harmless
(`env_int`/`env_float` fall back to the same code default on an empty value) but, unlike every
other numeric passthrough in that file, it hid the effective number from anyone reading it; the
defaults are now spelled out and pinned in `marceline/tests/test_compose_env.py`. And the try-it
commands below and in `docs/runbooks/production.md` §13 and `docs/plans/cloud-fingerprinting.md`
used to read `docker compose -f infra/compose.yaml …` without `--env-file .env`: with `-f`
pointing at a file under `infra/`, Compose looks for `.env` next to *that* file rather than in the
repo root the command is run from, and the trial's first `web` recreate that way silently lost
`WEB_BIND`, mail and Sentry settings.

**Try it** (development VPS, main checkout; every command below names `--env-file .env`
explicitly for the reason above — the same form `make`'s own `$(COMPOSE)` uses):

```bash
make up                                                        # web picks up the switch and mounts the spool
docker compose -f infra/compose.yaml --env-file .env --profile uploads up -d fingerprint-worker
docker compose -f infra/compose.yaml --env-file .env logs -f fingerprint-worker
```

then check the spool is empty once the queue drains:

```bash
docker compose -f infra/compose.yaml --env-file .env exec fingerprint-worker \
  ls -la /var/lib/marceline/upload-spool
```

Full steps, including the byte-level checks the trial ran: `docs/runbooks/production.md` §13.

The trial says the machinery works and keeps nothing, not that the feature may be switched on for
a real customer.

## Amendment 2026-09-19 — the relay is built, and the feature is narrowed to the sole performer

Two changes, one operator decision each, and they only make sense together.

**The decision.** Cloud fingerprinting must be available in the private beta **on the public host**
(`dash.marceline.studio`). The reason is the product's, not the architecture's: the creators this is
for arrive from Instagram, on a phone or on a five-year-old laptop, and "hash your 4K master in your
browser" is an entrance barrier they cannot pass. §1 above chose development-only for v1 and §9
designed the streaming relay that lifts it; the relay is now built. **Nothing about the retention
guarantees, the containment or the flag's default changes**, and the second half of this amendment
is what makes the feature defensible at all.

### A. The relay (§9, built)

`marceline/uploads/relay.py` on the public host, `/internal/v1/relay/uploads…` on development, and
**the same six operations** on both sides (`marceline/app/uploads.py` `begin`, `append`, `finish`,
`listing`, `read_one`, `cancel`) — never a second implementation, exactly as `register_fingerprint`
is the one registration function.

- **Production reads and forwards, in 64 KiB pieces.** `HttpRequest.read` streams from
  `wsgi.input`; each piece is yielded into the outgoing body, so a customer's bytes are in that
  process's memory for one socket read and never longer. `request.body`, `request.POST` and
  `request.FILES` are never touched, which is what keeps Django's multipart handlers — the only
  thing in Django that writes an upload to `FILE_UPLOAD_TEMP_DIR` — out of the path entirely.
- **Every layer streams, and that is asserted rather than assumed.** Caddy's `request_body` only
  *limits* a body (9 MiB, exactly the two chunk paths: `POST /app/api/uploads/*/chunks/*` and
  `POST /api/v1/app/uploads/*/chunks/*`); buffering would need `request_buffers`, and a
  `request_buffers` line anywhere in `infra/prod/Caddyfile` is now a test failure. The Next BFF
  already streams (`duplex: "half"`, ADR 0036). gunicorn has no request-body spooling at all. The
  sharpest proof is a test: a chunk relays successfully **while `DATA_UPLOAD_MAX_MEMORY_SIZE` is set
  below the chunk size and `FILE_UPLOAD_TEMP_DIR` is read-only** — anything that read the body would
  raise `RequestDataTooBig`, anything that spooled would raise `PermissionError`. Replacing the
  generator with `request.body` fails that test, which is how it was verified.
- **Reads relay too.** A status read is answered by the machine that holds the row, like every other
  read of customer data — it is simply not answered from a signed bundle, because a `MediaUpload`
  exists for minutes and a progress bar cannot be eventually consistent. Adding uploads to the
  bundle would have meant putting rows about a customer's file into the public host's database,
  which is the thing this design avoids.
- **The credential is its own.** A `delivery.ServiceToken` of the new scope `upload_relay`
  (`manage.py create_service_token --scope upload_relay`), presented to `/internal/v1/relay/…` and
  accepted nowhere else; the read-only export token is refused there and this one cannot read a
  bundle. It is **not** a database role and **not** the signing key. This is the widening of
  ADR 0028's "read-only export token" the operator accepted on 2026-09-18 (issue #59), and it is
  bounded three ways: the listener is reachable over Tailscale only, the scope reaches nothing but
  these six routes, and the credential may only append to, complete or cancel an upload **it
  created** — `MediaUpload.origin` makes that a query rather than a promise.
- **Who is acting is attested, and that is the residual risk.** Production authenticates the
  session and checks CSRF, then names the organisation and the user in two headers; development
  trusts them. So a stolen relay token could act for any customer of ours. Written down here as an
  accepted risk (operator, 2026-09-18) rather than mitigated by a signature scheme we would then
  have to hold a second key for.
- **Three places cannot disagree about where bytes may rest.** `core/deploy.cloud_upload_problems`
  refuses to start a process that has the flag without relay mode, or relay mode with a spool, or
  relay mode on development at all; `public_surface.dev_only_prefixes` keeps the upload routes 404
  on the public host **without** relay mode; and `infra/prod/check_config.py` refuses a compose file
  that hands the flag to anything but `web`, or to `web` without relay mode, an address and the
  token — and still refuses a spool setting, a spool volume and any decoder command anywhere.
- **What did *not* change**: the spool, the worker, the child process with no database credentials,
  `RLIMIT_FSIZE = 0`, the ffprobe allowlist, `-protocol_whitelist file`, the six no-retention
  guarantees, the customer's file name never being transmitted, and the flag's default (false, in
  all six places it is written).
- **One honest cost.** Development's Tailscale-only `export` listener is the relay's receiver, so it
  is now the **third** service that mounts the spool volume (`web`, `export`, `fingerprint-worker`),
  and its gunicorn gained threads and a long deadline because a relayed chunk is open for as long as
  the customer's uplink needs. A separate ingest process would have been cleaner and costs RAM this
  box does not have.

### B. The sole-performer declaration, and why the feature is narrower than it was

An upload contains personal data of everyone shown in it, and for a co-performer we have no
relationship with — often only a stage name reaches us — consent cannot be obtained. So the
feature is **narrowed**. An upload is accepted only where the uploader declares, per file:

1. that they are the **only** person visible or audible in it, and
2. explicit consent to the processing of that file to compute fingerprints.

Two statements, **two separate ticks**, one versioned declaration
(`marceline/app/upload_declaration.{py,json}`, version `upload-2026-09-19`), stored **verbatim** on
the `MediaUpload` with its version, its language and the moment it was made — modelled on the rights
declaration (ADR 0034) and the majority declaration (ADR 0052 amendment), and re-checked at
registration so a wording retired while the file was waiting refuses the job. German operative,
English a reading aid, both first person. No duration is promised: `banned_in` applies the wording
test's own bans to the server's words, because that test cannot see them.

**A file with other performers is not refused as a product** — it is pointed at the local,
in-browser route, where the bytes never leave the machine and we process nobody's data at all. That
sentence is on the screen, under the two boxes, in both languages.

What this costs, stated plainly: a studio's back catalogue is what §1's Context named as the
feature's reason, and most of that catalogue has several performers. **The narrowing may leave the
feature serving only self-producing solo creators** — which is also exactly the audience the
operator's decision above is about. It is a smaller feature than the one that was built.

A reviewed wording is a **new version** of the declaration, never an edit of this one.

### C. What an operator must do before the switch may move

In order, and none of it is a configuration change:

1. **The declaration wording (§ B) is reviewed**, and the contact point, terms paragraph and abuse
   address of issue #127 exist.
2. **The published claim changes first.** `Dein Video bleibt bei dir.` / `Your video stays with
   you.` is a headline **and** a `<meta name="description">` on both home pages, and
   `make website-check` fails while the claim stands
   and any default is true — the two move in one change or not at all.
3. **The spool's disk is encrypted** on the development VPS, and the answer is recorded (§3).
4. **The relay's credential is created** on development (`create_service_token --scope
   upload_relay`), `CLOUD_RELAY_BASE_URL` names development's Tailscale address, and both reach
   `web` only — `make prod-config` refuses every other shape.
5. Then, and only then, `CLOUD_FINGERPRINTING_ENABLED=true` **and** `CLOUD_UPLOAD_RELAY_MODE=true`
   in `infra/prod/.env`, with a recreate (a container keeps the environment it was created with).

`docs/runbooks/production.md` § 13 has the commands and the checks.

## Amendment 2026-09-20 — the attestation is bound, not believed

**The finding** (security review of the first release, severity major). §9's relay authenticates the
customer's session and checks CSRF on the public host, then *attests* the acting organisation and
user in two headers, and development believed them: it resolved the pair against a
`accounts.Membership` row and asked nothing else. So the `upload_relay` credential — a **write**
capability, held by the internet-facing `web` process — could act for any pair that happened to
exist, where ADR 0028 promised production a scoped, read-only export token. The amendment above
wrote that down as an accepted risk. This one narrows it.

**Three changes, all on development, none of them a new setting or a new key.**

1. **The pair is verified before a byte is read** (`marceline/uploads/attestation.py` `actor`).
   Uploads must be available on this machine at all — the same gate `app/uploads._customer` applies
   to a browser, so the relay can never do something a local customer could not — the two headers
   must parse, a `Membership` must link **exactly** that user to **exactly** that organisation, and
   the account must be **active**: a switched-off account's own session stops working, and a
   credential that outlives it would be a way back in. The per-file sole-performer declaration needs
   no new check and got none: the relay calls `uploads.begin`, which calls `validated`, which is
  where the consent declaration of §B is checked — a test now asserts that on the relay path rather than
  leaving it to be inferred.
2. **One credential may not quietly serve the whole customer base** (`attestation.admit`). The
   declare step — the only route that can admit an organisation the relay was not already serving —
   refuses once `MAX_ORGANISATIONS` (25) distinct organisations have started a relayed upload in
   24 hours. The private beta is a handful of creators and will not see it; a `web` walking the
   customer list meets it on the 26th. The refusal is the `503 uploads_unavailable` a customer
   already sees when this machine is not taking files, asked only after the pair has resolved, so it
   tells nobody anything.
3. **Every refusal is counted** (`marceline.RelayRefusal`, `marceline/0030`). Everything the
   credential does successfully already leaves a `MediaUpload` row; everything it did
   unsuccessfully left nothing, which is the difference between "nobody uses this" and "something is
   probing it". It is a **counter, not a journal**: one row per hour per reason, and no
   organisation, user or upload in it — a row that named the attested pair would grow without bound
   under exactly the caller it watches, and the pair is not what the alarm is about. Append-only in
   the database: no DELETE, and the only UPDATE that passes is the counter going up.

**And the reads narrow to match the writes.** §9 kept the credential's writes to rows of
`MediaUpload.origin = relay` and deliberately left `listing` and `read_one` wide, so a customer's
overview would show their uploads whichever door the bytes came through. On the public host there is
no other door — a customer's rows are relay rows by construction — so that costs nothing and the
invariant becomes one sentence: **the relay only ever touches rows it created.** An upload the
operator starts over Tailscale is now outside the reach of a credential the internet can talk to.

**What was considered and rejected: a signed upload grant.** The obvious stronger design is to bind
an upload to something `web` cannot mint — a short-lived, single-use grant that development signs
(production already holds development's public key) and the relay must present. It does not work
**in this topology**, and saying why is the point: the customer's browser talks only to production.
Any grant the customer could obtain, production obtains on their behalf over the same relay
credential — so a `web` that wanted to act for an arbitrary customer would simply ask for that
customer's grant first. It would add a second signing path, a nonce store and an expiry clock, and
move the trust boundary nowhere. **It would be theatre, and it is not built.** The design that would
actually close this is a secret the *customer* holds and development can verify without production
in the middle, which is a different product (client-side keys) and a different ADR.

**What this does not solve, plainly.** A compromised or malicious public `web` can still name a
**real, active, entitled customer** and, as them: start an upload, have arbitrary bytes fingerprinted
into that customer's account as an original, and attach that customer's identity to a rights
declaration and a consent record they never gave — and read the titles of that
customer's uploads, which is a cross-tenant read the relay needs in order to draw the screen at all.
What is removed is every pair that is *not* such a customer, the ability to spread quietly over the
whole customer base, and the silence. What is unchanged is that the honest mitigation for the rest is
the one ADR 0028 already names: the listener is Tailscale-only, the scope reaches six routes, the
credential is revocable in one row, and the feature is off by default behind two flags.

**No new setting, no new credential, no new dependency**, and nothing in `infra/` moves.
`CLOUD_FINGERPRINTING_ENABLED` and `CLOUD_UPLOAD_RELAY_MODE` keep their defaults, and §C's
preconditions are untouched.

## Consequences

- The product's core sentence becomes conditional: "your video never leaves your machine — unless
  you choose to upload it". Every place that states it (the marketing site, the privacy notice)
  has to say the conditional version from the moment the flag can be switched on.
- One more long-running process type on a box that is already RAM-tight, and one more image to
  rebuild. It is in its own compose profile and off by default, so `make up` and `make workers` are
  unchanged.
- `register_fingerprint` becomes the single registration function with three callers. That is a
  refactor of the most sensitive customer write in the system; it is covered by the existing
  `test_app_fingerprints.py` and `test_customer_writes.py` suites, which must stay green unchanged.
- A new never-do rule for agents: **uploaded media never touches Postgres, a log, a backup or a
  Sentry event, and the decoder never gets database credentials or a network.**
- We accept that "deleted" is provable only as process evidence (a receipt, an append-only row, a
  volume outside the backups, a reaper) and never as a negative. That is what the customer-facing
  wording is allowed to claim, and no more.

## Alternatives rejected

- **tus / tusd**, `docs/research/cloud-fingerprinting.md`'s recommendation: a second server and a
  hook protocol for resume semantics a fixed chunk size gives us for free. Revisit if resumability
  across days, not reloads, ever matters.
- **S3-compatible object storage with SSE-C**: pays for a bucket, a key scheme, a lifecycle rule
  and a second place where the tenancy check could be wrong, to move bytes we intend to delete
  within minutes. The research doc's own cost table puts storage under €10/month — the cost was
  never the objection, the second copy of the data was.
- **Hashing in a browser Web Worker but uploading on failure only** (a "fallback" that hides the
  upload): the customer would not know which of their files left their machine. Opt-in per upload,
  visible, or not at all.
- **A separate ingest VPS now**: real hardware, a real bill and a third machine to patch, for a
  feature behind an off flag.
- **Streaming the upload straight into ffmpeg with no spool at all** — the strongest possible
  no-retention story, and it does not work: a container whose index sits at the end of the file
  cannot be decoded from a pipe, and that is most of what customers shoot and edit. Keeping it for
  the files where it *does* work would mean two pipelines and two answers to "where were my bytes".
