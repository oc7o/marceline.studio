# 0020 — Photo-collection references

Status: accepted (2026-09-16)

Extends ADR 0010 (creator references) and ADR 0015 (frame evidence). Issue #69.

## Context

A `matching.Reference` was one video: frames along a timeline, each hash carrying
`frame_index / fps` seconds. Creators also sell photo sets, and those leak — usually the whole
set at once, reposted as a gallery page. PDQ needs no change; it is an image hash and already
hashes page posters and hover frames. What does not fit is everything the video shape assumes:
a duration, a sampling rate, an order between frames, and the evidence rules built on that order
(timeline verification, the loose-distance admission, the two-frame floor under the high band).

## Decision

### One reference per collection, not per image

A photo collection is **one** `Reference` with `kind = photos` and one `ReferenceFrameHash` per
image (times eight dihedral orientations, as for video). One reference per image was rejected:

- A leak reposts the set, so the interesting unit of review is "this page shows the set", not
  "this page shows photo 7". With one reference per image, one gallery page would create N
  matches, N review-queue rows and N leak decisions for a single human judgement.
- Everything already keyed by reference works unchanged: `ReferenceMatch` stays unique per
  (reference, page), `suggest_leaks` ranks per original, `m2_report` counts reference/page pairs,
  `ReferenceCandidate` caches per reference. Per-image references would multiply all of them.
- Per-image references would also lose the evidence that matters most — that *several different
  photos of the same set* appear on one page — because each match would only ever see one image.

Checked against the code before choosing: nothing in `matching` reads `Reference.frames` as a
time series except the timeline (ADR 0015) and the mm:ss rendering in admin and the review API.
Both are handled below.

The cost is the collection identity. `Reference.sha256` is unique and was the file's digest; a
collection has no single file, so it stores a **collection digest**:
`sha256("marceline-photo-collection-v1\n" + "\n".join(sorted(image sha256 digests)))`
(`matching.fingerprint.collection_sha256`). It is order-independent and duplicate-sensitive, and
the domain prefix keeps it from ever colliding with a file's sha256. Known limit: adding or
removing one image makes a different collection, which imports as a second reference. That is the
safe direction — the alternative is silently mutating an original's hash set — but the operator
should re-import a changed set deliberately and delete the old row.

`frame_index` is the image's ordinal in sorted path order. `timestamp_s` repeats that ordinal
because the column is not nullable and every consumer reads it; it is a **position, never a
time**, and anything rendering it as mm:ss must check `Reference.kind` first (admin, the review
API and the moderation UI now do). `duration_s` and `frame_fps` are NULL, enforced by the
database: `reference_shape_matches_kind` requires `kind='video' AND frame_fps > 0` or
`kind='photos' AND frame_fps IS NULL AND duration_s IS NULL`.

### Pipeline tag `pillow-rgb-pdq-v0`

Photo collections are hashed by Pillow at the image's own resolution, converted to RGB, and
passed to PDQ — no ffmpeg, no 512-pixel scaler, no timeline. That is exactly what
`matching.frames.hash_poster` does to a page image, so both sides of a photo match go through the
same recipe. It is *not* the video recipe, so it gets its own tag, as the browser client did
(`browser-rgb512-pdq-v0`, ADR 0019).

`PIPELINES_BY_KIND` makes the tag a type: `from_json` refuses a photo pipeline on a video
document and a video pipeline on a photo document, so an image reference and a video reference
can never be confused, whatever a file claims. EXIF orientation is deliberately not applied: the
eight dihedral variants already cover rotation and mirroring, and applying it would move the
reference hash away from how the leaking site's copy is hashed.

### Fingerprint format version 3

`to_json` writes version 3 for both kinds; `from_json` still reads version 1 and 2, which are
videos by definition. Version 3 adds a required `kind` (`video` / `photos`). A photo document
replaces `duration_s` / `fps` with `image_count` (1..20 000) and its frames carry no
`timestamp_s` at all — the position of an image is its ordinal and nothing else. Key sets stay
exact per kind, so a video document carrying `image_count`, or a photo document carrying a
timestamp, is refused rather than half-understood. `client` records `pillow` instead of `ffmpeg`,
because that is the decoder whose version a later parity check needs.

Hashes-only stays hashes-only: sha256, size, image count, title, creators, software versions and
PDQ hashes. No image data, no file names, no paths, no host or user names. The operator's files
are read in place, once, and never copied: the same bytes produce an image's sha256 and its PDQ
hashes, so the collection digest can never describe bytes that were not hashed.

### CLI and import

`client/fingerprint.py --photos` turns every folder on the command line into one collection file
(`<title>.<first 12 of the collection digest>.json`), recursing into subfolders and skipping
hidden and `._*` entries. Accepted suffixes are what the server's Pillow 12.3 actually decodes,
kept in `matching.fingerprint.IMAGE_SUFFIXES` so both sides agree: `.jpg .jpeg .png .webp .avif`
(HEIF is not compiled in, JPEG 2000 and TIFF are but are not offered). The title defaults to the
folder's name — a name, never a path. ffmpeg is not required in this mode.

`import_fingerprints` imports collection files through the same atomic `import_fingerprint` as
videos, so several creators per collection work exactly as issue #64 requires: `--creator` is
repeatable, the first name is the rights holder, `matching.creators.set_creators` writes the
whole cast inside the import transaction.

### Evidence and bands — the part that needed a decision

Two things make a collection different from a video:

1. **There is no order**, so timeline verification has nothing to verify. A collection match
   never computes a timeline: `timeline_score` is NULL with the reason "photo collection: images
   are independent, no timeline". NULL is already *neutral* in confidence (ADR 0015), so a
   collection is never penalised for what it cannot have. The loose-distance admission (32..40)
   consequently has only one route left for collections — cluster/mirror corroboration — because
   its other route is a consistent timeline chain. That is deliberate: without an order there is
   nothing to make a far-away hash trustworthy.

2. **Each image is independent evidence.** For a video, two evidence frames mean two different
   moments of the same film on the same page, and a timeline can tie them together. For a
   collection, one matched image is exactly one picture, with nothing corroborating it — weaker
   than one frame inside a consistent chain. The video weights were therefore **not** reused
   unchanged.

The unit of collection evidence is the **independent (page frame, collection image) pair**:

```
collection_images = min(distinct matched collection images, distinct evidence page frames)
```

The cap in both directions is the point. One page picture matching three near-identical photos of
the same shoot is one piece of evidence, not three. Three page frames all showing the same photo
(a poster repeated as a hover frame) are one piece of evidence, not three.

- **Confidence** gains one named component, used only for collections:
  `images = 0.20 × (1 − 0.5^(collection_images − 1))` — 1 image adds 0, 2 add 0.10, 3 add 0.15,
  4 add 0.175. It stands in for the two signals a collection structurally cannot earn (a verified
  timeline and a matching duration) and is the same size as the timeline weight, so a collection
  is never scored more generously than a video. Without it a collection could not reach the high
  band at all: its ceiling would be 0.45 + 0.20 + 0.10 = 0.75, the threshold itself.
- **Band `high`** requires the usual confidence threshold **and at least 3 independent image
  pairs** (`HIGH_MIN_COLLECTION_IMAGES`), where a video requires 2 evidence frames
  (`HIGH_MIN_EVIDENCE_FRAMES`). The floor says what the evidence must be made of, not how good it
  looks.
- **Strength** follows the same unit: a collection match is `strong` with at least
  `STRONG_MIN_FRAMES` (2) independent image pairs, not merely 2 page frames.

What that means at the default thresholds (high 0.75, review 0.40), for a page with no duration
signal and no corroboration:

| Evidence on the page | Confidence | Band |
|---|---|---|
| 1 image, distance 0 | 0.525 | review |
| 2 images, distance 0 | 0.7375 | review (floor also blocks high) |
| 3 images, distance 0 | 0.8438 | **high** |
| 3 images, distance 10 | 0.7938 | **high** |
| 3 images, distance 20 | 0.7438 | review |
| 4 images, distance 20 | 0.7969 | **high** |

So a single leaked photo is always a human decision, two photos are a strong hint that still gets
reviewed, and three photos of the same set on one page — which is what a reposted set looks like
— is the automatic high band. `AUTO_CONFIRM_HIGH` is off by default, as before; nothing is
confirmed silently.

`calibrate_confidence` re-bands video matches in one UPDATE and collection matches row by row,
because their floor lives in `matched_seconds` (which images matched) rather than in a column.
Both use the same `band_for`, so a stored band always equals what `match_references` would write.

## Consequences

- **`make migrate` and a worker restart (`make workers`) are required** after merging
  (`matching/0015_reference_photo_collections.py`). Worker processes keep the code they started
  with; after a model change they fail every write (CLAUDE.md, 2026-09-15).
- Shared-frame detection, common-frame discounting, clustering, incremental scanning, leak
  suggestion and the M2 report needed no change: they only ever compare hashes.
- Duration signals are silently absent for collections (`duration_s` is NULL), which also means
  `suggest_leaks` proposes no duration-only candidates for them and `triage_browser_capture`
  never queues one for an exact-duration match. Both are correct: a photo set has no runtime.
- The browser upload endpoint (`POST /api/v1/originals`, ADR 0019) is unchanged and still a
  video path; the browser client writes version 2 video documents, which remain valid.
- The weights are hand-set, like every weight in ADR 0015, and the table above is arithmetic, not
  a measurement. The first real collection import should be reviewed by hand before anyone trusts
  the high band for photos; `calibrate_confidence` keeps the defaults until 20 labeled leaks
  exist either way.
