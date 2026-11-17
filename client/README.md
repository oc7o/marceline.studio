# Local fingerprint client

Fingerprint purchased originals on your Mac instead of uploading multi-GB files. The client writes
one small JSON file per video — or one per photo collection with `--photos` (hashes and metadata
only); copy those to the server and import them. It runs the server's own hashing code
(`backend/matching/fingerprint.py`, no Django). The only network access it can make is the one-off
`--fetch-model` download for the second signal (ADR 0037); without that flag it never uses the
network at all. Issues #27 and #69.

## Prerequisites (once)
```sh
brew install ffmpeg uv
git clone git@github.com:oc7o/marceline.studio.git
cd marceline.studio
```
`uv` installs Python 3.13, numpy, pdqhash, Pillow and onnxruntime on the first run. Update later with `git pull`.

The server needs the matching code too: `git pull`, `make migrate`, then `make workers` there.

## Fingerprint a folder
```sh
uv run client/fingerprint.py ~/Originals --creator "NAME"
```
Videos with several creators repeat `--creator`; the first name is the primary creator (the
rights holder), the rest are co-creators:
```sh
uv run client/fingerprint.py ~/Originals/studio-video.mp4 \
  --creator "Sasha Meyer" --creator "Carla Bremer"
```
| Option | Default | What |
|---|---|---|
| `PATH …` | | video files or folders (recursive: mp4 mov m4v mkv webm avi wmv mpg mpeg ts; hidden and `._*` files skipped) |
| `--photos` | | treat every folder as one photo collection instead of looking for videos |
| `--out DIR` | `./fingerprints` | output folder |
| `--creator NAME` | | repeatable: primary creator first, then co-creators; stored in the files (or pass them at import) |
| `--title TITLE` | file or folder name | only with a single video or collection |
| `--fps N` | `1.0` | samples per second, videos only; `2` only for the denser re-fingerprint below (files get a `.2fps` suffix) |
| `--no-dihedral` | | only the original orientation (default: all 8) |
| `--embeddings` | | also store one 512-bit embedding sketch per moment (ADR 0037); files get an `.emb` suffix |
| `--crop-grid` | | also store 256 crops of every moment (ADR 0039), videos only; files get a `.grid` suffix. **Hours, not minutes** — see below |
| `--crop-grid-dedup BITS` | off | prune crops within BITS of one already kept (24 halves the blob for 0.03 of recall) |
| `--fetch-model` | | download and check the embedding model once per machine, then exit |
| `--jobs N` | half the CPU cores | videos or collections hashed in parallel |

Output: `fingerprints/<name>.<first 12 of sha256>.json`, written atomically. Re-running skips videos
that already have a file, so an interrupted run (Ctrl-C, sleep) just continues. Progress, a summary
and the next commands go to the terminal. Exit code 1 if any video failed.

## Fingerprint a photo collection
A photo set is **one** original: the leak reposts the set, not a single picture (issue #69,
`docs/adr/0020-photo-collection-references.md`). One folder becomes one JSON file.
```sh
uv run client/fingerprint.py --photos ~/Originals/photos/beach-set --creator "NAME"
```
Several folders in one run are several collections; loose image files named on the command line
become one collection together. Images are found recursively: `jpg jpeg png webp avif` — what the
server's Pillow actually decodes (HEIC is not supported, convert it first). Hidden and `._*` files
are skipped, and ffmpeg is not needed in this mode.

The collection's identity is a digest over its images' digests, so the file is named
`fingerprints/<folder name>.<first 12 of the collection digest>.json` and a re-run skips it.
**Adding or removing one image makes a different collection**, which imports as a *second*
original — re-import a changed set deliberately and delete the old one in the admin.

Each image keeps only its position in the set (`0`, `1`, `2`, …); there is no time, no duration
and no sampling rate, because a collection has no timeline. Matching does not penalise that: a
page showing photos of the set in any order is scored on *how many different photos* it shows.

**A customer can add a photo set in the app too** since issue #116 (ADR 0053): the browser hashes
it with the same recipe and writes the same document, under its own pipeline tag
`browser-rgb-pdq-v0`, because a browser's image decoder is not Pillow's and nobody has measured the
gap yet. This CLI stays the path for large sets (up to 20,000 images against the app's 5,000), for
anything a browser will not decode — HEIC, which neither side reads — and for an import where the
files are already on the machine that does the hashing. Both sides compute the **same collection
digest** from the same images, so the two imports are two references of one set, exactly as a
re-fingerprinted video is (`import_fingerprints --compare` reports the pipeline mismatch, which is
the point rather than a problem).

## The crop grid: `--crop-grid`

A site does not republish your frame, it republishes a **re-framed** copy — 1-3 % trimmed off the
edges, asymmetrically — and that alone costs PDQ 44-70 bits, which is more than the whole match
budget. `docs/matching/reference-crop-grid.md` measured it on 160 real film frames: a single hash
finds 0.02 of 3 % re-frames inside the threshold, a lattice of **256 crops per moment** finds 0.95,
and over 805,683 unrelated pairs not one came within 48 bits of any crop. The threshold is 31.

Because we own the originals, the lattice goes on **our** side and the page side costs nothing:
every page hash already stored is compared against it, with no re-fetch and no re-hash.

```sh
uv run client/fingerprint.py ~/Originals/batch-1 --creator "NAME" --crop-grid --out fingerprints-grid
```

**What it costs.** Every sampled second is hashed 256 times instead of once. Measured with the
shipped code: **2.4 s of CPU per sampled second** from a 1280-wide master and **5.5 s** from a
1080p one, on the development VPS. For the current 44 originals (76,795 sampled seconds), mostly
1080p, that is on the order of 100 core-hours — about **12-15 h on 8 cores**, not the 5 h ADR 0039
estimated from 720p masters. Run it in batches, overnight, with `--jobs`. On disk it is 8.2 KB per
moment (629 MB for the library) and roughly twice that in the JSON files, which store the blobs as
hex. The client prints seconds per frame on every finished video, so the first video tells you what
the rest will cost on *your* machine.

`--crop-grid-dedup 24` halves the blob (a median of 139-151 of 256 crops survive) for 0.03 of
recall at a threshold of 31 and no measurable precision. Not worth it at 44 originals; worth it
before the library reaches a few hundred.

**It is a different recipe, so it is a different pipeline tag.** The frames come out of ffmpeg at
their master width and are scaled in Pillow, because cropping the 512 px picture instead of the
master throws away a median of 8 bits for nothing. So a gridded file imports **alongside** the
existing reference (`ffmpeg-master-pil512-pdq-grid256-v1`), exactly like a 2 fps re-fingerprint,
and nothing that already exists changes. The original must be at least 1024 px wide; a narrower
one is refused rather than given a worse grid.

**Nothing uses the grids until the operator switches them on** (`MATCHING_REFERENCE_CROP_GRID`),
and adoption also means re-running `calibrate_confidence` on the server: a minimum over 256
correlated hashes moves every distance down 10-16 bits. The pilot
(`manage.py crop_grid_pilot`) decides that, on two originals first.

## What the JSON contains
Format `marceline-fingerprint` v5: a `kind` (`video` or `photos`), the sha256 and total size, the
title (file or folder name), the creators as a list (primary creator first), software versions
(tool, OS, CPU architecture, Python, ffmpeg or Pillow, pdqhash) and 8 PDQ hashes with a quality
score per position. A video adds its duration, its sampling rate and a timestamp per frame; a
photo collection adds its image count and carries no time anywhere. A gridded video (v5) also
carries the lattice it was cut on and, per moment, the whole blob as hex — which is what makes such
a file large. No video or image data, no frames, no file paths, host or user names.
The server still imports v1 and v2 files, which are videos (v1 held a single `creator` or none).

The hashes still identify the video or the photos: treat the files as sensitive and delete them on
both machines after the import.

## Copy and import
```sh
rsync -av fingerprints/ devbox:marceline-fingerprints/
ssh devbox
cd ~/marceline/backend && uv run python manage.py import_fingerprints --dry-run --creator "NAME" ~/marceline-fingerprints/
cd ~/marceline/backend && uv run python manage.py import_fingerprints --creator "NAME" ~/marceline-fingerprints/
rm -r ~/marceline-fingerprints
```
Run the `--dry-run` first: it validates every file and prints what would be imported. The import
prints one line per file (`imported reference N`, `skipped (already imported as reference N)`,
`ignored: <reason>` or `error …`), continues past bad files and exits non-zero if any failed.
`--creator` is repeatable and overrides the whole cast stored in the files, first occurrence
primary:
```sh
cd ~/marceline/backend && uv run python manage.py import_fingerprints \
  --creator "Sasha Meyer" --creator "Carla Bremer" ~/marceline-fingerprints/scene.json
```
An unknown or ambiguous name fails that file and imports nothing for it.

**Permanently ignoring a file.** A file whose sha256 is listed in `IGNORED_FILES`
(`backend/marceline/management/commands/import_fingerprints.py`) is always skipped — on a real
import, `--dry-run` and `--compare` alike — so a re-upload by accident (the file still sits in
your local fingerprints folder) imports nothing. To ignore a file, add its sha256 (the JSON's
`sha256` field, or the digest prefix already in the filename) and a short reason to that list on
the server and commit it; to stop ignoring it, remove the entry.

Matching happens on the scheduler's next `match_references` run (every 3 h), or immediately with
`cd ~/marceline/backend && uv run python manage.py match_references`.

## Re-fingerprint the library after a pipeline change

The **pipeline tag** records how a set of hashes was made. It changed on 2026-09-16 from
`ffmpeg-rgb512-pdq-v0` to `ffmpeg-rgb512-pdq-v1` (issue #70,
`docs/adr/0022-frame-sampling-grid.md`): the old recipe hashed the picture about half a second
*after* the second it labelled it with, the new one hashes the picture at that second.

Old references keep working. Nothing is deleted or reinterpreted, and an original whose source file
is gone can simply stay on v0. But a re-run gives the newer, correct grid, so re-fingerprint what
you still have — 39 originals, comfortably done in batches of five or ten.

**See what is still on the old grid.** On the server:
```sh
cd ~/marceline/backend && uv run python manage.py reference_report
```
Every original prints a `pipeline:` line, and the end of the report counts them per pipeline. The
admin *Matching → References* list has the same column and a **Pipeline** filter in the sidebar.

**Per batch, on your Mac.** Use a fresh output folder so a batch is self-contained; the client
skips nothing here, because a v1 file has a different name from no file at all.
```sh
uv run client/fingerprint.py ~/Originals/batch-1 --creator "NAME" --out fingerprints-v1
rsync -av fingerprints-v1/ devbox:marceline-fingerprints-v1/
```

**On the server, look before you import.** `--compare` finds the existing v0 reference for the same
file, prints the distance between the two grids and imports nothing:
```sh
cd ~/marceline/backend && uv run python manage.py import_fingerprints --compare ~/marceline-fingerprints-v1/
```
A **large** distance is the expected result here — it is the grid moving, not a fault. What you are
checking is that every file finds its existing reference and that none of them errors.

**Import.** The v1 file lands *beside* the v0 reference; both exist, both match:
```sh
cd ~/marceline/backend && uv run python manage.py import_fingerprints --dry-run --creator "NAME" ~/marceline-fingerprints-v1/
cd ~/marceline/backend && uv run python manage.py import_fingerprints --creator "NAME" ~/marceline-fingerprints-v1/
cd ~/marceline/backend && uv run python manage.py match_references
```
Each line says `imported reference N (alongside reference M on ffmpeg-rgb512-pdq-v0 at 1 fps)`. Note both
numbers — N is the new original, M is the one to retire.

**Confirm, then retire.** Compare the new original's matches against the old one's in the review
queue or with `reference_report`. Once the v1 row finds at least what the v0 row found, delete the
v0 reference in the admin (*Matching → References*, filter Pipeline = `ffmpeg-rgb512-pdq-v0`).
Deleting it removes its hashes and its match rows; review decisions on the old row are **not**
carried over to the new one, so retire a batch soon after confirming it rather than leaving both
grids running for weeks. Then delete the JSON files on both machines and start the next batch.

If you are unsure about a batch, do nothing: two references for one original is a safe state — it
costs a duplicate row in the queue, never a missed leak.

## Denser sampling: re-fingerprint at 2 fps

**Status: prepared, not decided.** The operator decides whether and when to run this
(`docs/adr/0022-frame-sampling-grid.md`, amendment 2026-09-17).

Why: many leak pages show only one poster, and a poster matches only when it sits within about a
quarter of a second of a sampled moment. At 1 fps that is a lottery (issue #73: the v0 and v1
grids of the same file each found 6 of 8 poster pages, together 8 of 8). The matching review
estimates 2 fps at about 85–100 % poster recall. 4 fps waits for a separate decision: it quadruples
the reference hashes and the scan time.

The sampling rate is part of a reference's identity (sha256, pipeline **and** fps), so a 2 fps run
of an original already imported at 1 fps imports **beside** it, exactly like a pipeline change.

**On your Mac**, same originals, `--fps 2`. The client names these files
`<name>.<sha256>.2fps.json`, so a folder that already holds the 1 fps files skips nothing; a fresh
`--out` folder is still tidier:
```sh
uv run client/fingerprint.py ~/Originals/batch-1 --fps 2 --out fingerprints-2fps
rsync -av fingerprints-2fps/ devbox:marceline-fingerprints-2fps/
```
Each video takes roughly twice as long and its file is twice as large.

**On the server, look first.** `--compare` picks the existing reference of the same file on the same
pipeline and compares the moments both grids sample (every second): expect **distance 0 for
(almost) 100 %** of the shared pairs on the same machine, since those are the same pictures. A
large distance means a different decoder or a different file, not a denser grid — stop and check.
An original that is still only on `ffmpeg-rgb512-pdq-v0` is compared against that row instead, and
there a large distance is expected (the pipeline note says so): the 2 fps run is also its v1
re-fingerprint.
```sh
cd ~/marceline/backend && uv run python manage.py import_fingerprints --compare ~/marceline-fingerprints-2fps/
```

**Import.** No `--creator` needed: the cast is inherited from the existing reference of the same
file and the line says so. Each line reads `imported reference N (alongside reference M on
ffmpeg-rgb512-pdq-v1 at 1 fps) (cast from reference M: …)`:
```sh
cd ~/marceline/backend && uv run python manage.py import_fingerprints --dry-run ~/marceline-fingerprints-2fps/
cd ~/marceline/backend && uv run python manage.py import_fingerprints ~/marceline-fingerprints-2fps/
cd ~/marceline/backend && uv run python manage.py match_references
```
Importing no longer blocks review or matching for minutes (the shared-frame check is batched), and
`match_references` scans only the new references against all page frames.

**Confirm, then retire the 1 fps row** in the admin, as after a pipeline change: once the 2 fps
reference finds at least what the 1 fps one found (`reference_report`, review queue). Until then
both match and a page can appear twice in the queue — safe, never a missed leak. A v0 reference of
the same file is retired the same way; the 2 fps v1 reference replaces both.

## The second signal: re-fingerprint with `--embeddings`
A frame embedding is a second, independent content signal beside PDQ, and it is the answer to the
transform that actually costs us recall: a site trimming 1-3 % off the edges asymmetrically costs
PDQ 44 bits and the embedding almost nothing (`docs/matching/second-signal.md`,
`docs/adr/0037-frame-embeddings-as-a-second-signal.md`). It is **never** evidence on its own — it
only proposes candidates a human or a PDQ match then confirms.

Originals live on your Mac, so their sketches have to be computed here. It needs the model once:
```sh
uv run client/fingerprint.py --fetch-model
```
That downloads 45 MB into `~/.cache/marceline/models/` and refuses anything whose sha256 is not the
pinned one. Then re-fingerprint the same originals **with** embeddings:
```sh
uv run client/fingerprint.py ~/Originals/batch-1 --embeddings --out fingerprints
rsync -av fingerprints/ devbox:marceline-fingerprints/
```
Cost: about 55 ms per sampled moment on top of PDQ, so roughly one extra minute per 1,000 sampled
seconds of video (the 44 originals are ~68 min of CPU in total). The files are named
`<name>.<sha256>.emb.json`, so a folder that already holds the plain files skips nothing.

On the server the import **adds the sketches to the reference that is already there** — the frame
grid did not change, so the original keeps its identity and its review decisions, and no second
reference appears:
```sh
cd ~/marceline/backend && uv run python manage.py import_fingerprints --dry-run ~/marceline-fingerprints/
cd ~/marceline/backend && uv run python manage.py import_fingerprints ~/marceline-fingerprints/
```
Check the result with `uv run python manage.py embedding_pilot --stage plan` on the server: its
first line should read `Reference side: 44 of 44 originals carry sketches (…)`.

It prints `added 1234 mobileclip-s0-fp32-sign512-v1 sketches to reference N`. Before it writes
anything it compares **every** position's PDQ hash with the stored one bit for bit, and refuses the
file if a single one differs — a sketch must never describe a moment the stored hash never saw. A
`--fps 2` run with `--embeddings` is a different grid and still imports as its own reference, as
before.

Nothing about matching changes until the operator switches the page side on: page sketches only
exist for media fetched after that, because page pictures are discarded after hashing.

## Hash parity (check once)
Homebrew's ffmpeg and Apple Silicon may decode or scale a few pixels differently from the server,
so hashes can differ by a few bits (matching tolerates distance 31). Check once with an original
that is already imported on the server (`add_reference`): fingerprint it on the Mac, copy the JSON
and run
```sh
cd ~/marceline/backend && uv run python manage.py import_fingerprints --compare ~/marceline-fingerprints/<file>.json
```
It prints the shared (frame, orientation) pairs, the share at distance 0 and p50/p90/p99/max
distances, and imports nothing for that file. Distances of a few bits are fine; if p90 is above
~10, keep importing originals on the server with `add_reference` and note the result in issue #27.
