# 0053 — Photo originals in the customer app

Status: accepted (2026-09-18)

Extends ADR 0020 (photo-collection references), ADR 0019 (the browser fingerprinting client) and
ADR 0022 (a pipeline tag is the record of which recipe produced a hash). Issue #116.

## Context

The primary audience is now the independent creator, who sells **photo sets** as much as videos.
Everything behind the browser already supports them: ADR 0020 made a collection one `Reference`
with `kind = photos`, one `ReferenceFrameHash` per image, a collection digest instead of a file
digest, and its own evidence rules; `client/fingerprint.py --photos` fingerprints a folder on the
operator's Mac and `import_fingerprints` imports it.

What does not support them is the part a customer touches. `POST /api/v1/app/fingerprints` refuses
anything that is not a video on the browser's video tag, the "Add an original" screen opens a file
picker for one video file, and the customer's words — the declaration, the extent explanation, the
evidence paragraph of a notice draft — say *video*. ADR 0019's amendment named this and left it
open: "Photo collections stay CLI-only: a browser photo recipe needs its own tag and a parity
measurement against Pillow (JPEG decoders and colour management differ), which is a follow-up."

This is that follow-up.

## Decision

### 1. The browser gets its own photo pipeline tag: `browser-rgb-pdq-v0`

The recipe is the same *shape* as the CLI's photo recipe (`pillow-rgb-pdq-v0`): the image at its
own resolution, converted to RGB, hashed by PDQ in all eight dihedral orientations. No scaler, no
timeline, no EXIF transform — `matching.pdq.hash_image` does exactly this to a page poster, which
is what both sides of a photo match must go through.

It is not the same *decoder*. Pillow decodes a JPEG with libjpeg-turbo's IDCT and chroma
upsampling and applies no colour management; a browser decodes with the platform decoder and may
apply the file's ICC profile. `createImageBitmap(blob, { imageOrientation: "none",
colorSpaceConversion: "none", premultiplyAlpha: "none" })` asks for none of that, but the first two
are *hints* the specification lets an implementation ignore, and a `getImageData` round trip
un-premultiplies alpha, which is lossy where a PNG is partly transparent. PNG is deterministic and
should agree bit for bit; JPEG, WebP and AVIF are the open question.

Per ADR 0022 that is precisely what a tag is for: **a recipe nobody has measured never borrows
another recipe's tag.** `browser-rgb-pdq-v0` records "these hashes came out of a browser's image
decoder". The alternative — claiming `pillow-rgb-pdq-v0` — would put an unmeasured decoder gap into
the one field that exists to record it.

**Bit-parity where it can be asserted.** Given identical pixels the two sides are already
bit-identical: the WASM hasher reproduces `pdqhash.compute_dihedral`, including the quality
integer, on committed fixtures (ADR 0017). `frontend/src/lib/photoFingerprint.test.ts` extends that
from the hash to the whole document: `backend/matching/tests/fixtures/photos/collection.json` is
written by `matching/tests/generate_photo_fixtures.py` from synthetic images (gradients, noise,
checkerboards, a near-uniform low-quality frame, odd and non-square sizes — no real media, ever),
and the test asserts that the browser code, fed the same pixels, produces the same eight hashes per
image, the same quality gate, the same ordinals, the same `image_count` and **the same collection
digest**. What remains untested there is the decoder, which is the one thing a unit test cannot
reach, and which is what the parity harness measures.

**The decoder gap is a measurement, on demand.** `make browser-parity PARITY_ARGS="--photos"`
(`frontend/parity/parity.py`) generates synthetic images with Pillow, hashes them with
`matching.fingerprint.fingerprint_photos` — the server's own code — and with
`fingerprintPhotos()` in a real headless Chromium, and reports the per-image Hamming distance by
image format. It is not part of `make check`: it needs a browser download (`frontend/parity/
README.md`, "Cost, honestly"). **No number is claimed in this ADR** — running it needs a browser
this machine may not download, and an unmeasured number is exactly what the tag protects against.
When the operator runs it, the table belongs in this section, as ADR 0019's does.

### 2. Everything CPU-bound happens in the Worker, one image at a time

A video needs the page, because an `HTMLVideoElement` exists only there. An image does not:
`createImageBitmap` and `OffscreenCanvas` work in a Worker, so `fingerprintPhotos()` hands the
Worker one `File` at a time and it does all of it — read the bytes, digest them, decode, hash,
release. Consequences that matter:

- **Memory is bounded by one image**, not by the set. The bitmap and the canvas are closed before
  the next file is read; nothing accumulates but 8 × 32 bytes of hash per image.
- **There is no object URL and no `<img>` anywhere.** The customer app is incapable of showing a
  picture (issue #53) and this feature does not become the exception — not even for the customer's
  own photos. A preview would need an element the privacy test bans, and "it is only their own
  file" is not a distinction the *code* can enforce once such an element exists. The screen shows a
  count and, at most, per-file failures; `frontend/customer/lib/privacy.test.ts` and its SPA twin
  stay exactly as strict as they were.
- The same bytes produce an image's sha256 and its PDQ hashes, as in `fingerprint_photos`, so the
  collection digest can never describe bytes that were not hashed.

**The collection's identity is the CLI's, to the byte:**
`sha256("marceline-photo-collection-v1\n" + "\n".join(sorted(image sha256 digests)))`. Sorted by
digest, so the order the customer picked the files in is irrelevant; duplicate-sensitive, so two
copies of a photo are two images. `frame_index` is the image's ordinal **in sorted-digest order**,
which is where the browser differs from the CLI in a way that does not matter: the CLI orders by
sorted *path* and a browser has no paths (and must not send one). An ordinal is a position and
nothing else (ADR 0020), no consumer compares two originals' ordinals, and the digest — the
identity — is order-free either way.

**A per-file failure never stops the set.** An image the browser cannot decode (HEIC is the common
one: Chrome does not decode it, Safari does) is counted and named by *format*, never by file name,
and the rest of the set is hashed. The screen then says plainly how many were skipped and offers
the practical way out — export the set as JPEG, or send it through the operator. A set where
*nothing* decoded fails as `no_usable_frames` and is reported through the existing
`client_failed` / `codec_unsupported` counters (ADR 0019), which are the input for #59.

### 3. The API accepts a photos document on the existing endpoint, behind no flag

`POST /api/v1/app/fingerprints` validates `kind` first and then the shape that kind requires:

| | video | photos |
|---|---|---|
| pipeline | `browser-rgb512-pdq-v1` | `browser-rgb-pdq-v0` |
| rate | `fps ∈ SUPPORTED_FPS` | no rate; `frame_fps`/`duration_s` are NULL by constraint |
| size cap | `MAX_POSITIONS` = 10,800 sampled positions | `MAX_IMAGES` = 5,000 images |
| refusal when over | `too_long` | `too_many_images` |

Everything else is shared and unchanged, because it is the same function: all eight orientations
required, at least one position that reached the quality threshold, a rights holder named, printable
title and names, the per-organisation rate limit, the owner taken from the session, the idempotent
200 for a collection this organisation already has, the single generic 409 for a claim on someone
else's material, the `needs_review` cast mapping, the rights declaration, and on production the
outbox.

**`MAX_IMAGES` = 5,000** comes from the route's own 16 MiB body cap, not from taste. A photo
document's frame row is `{"frame_index":N,"dihedral":D,"quality":Q,"pdq":"<64 hex>"}` — no
`timestamp_s`, as ADR 0020's format requires — which is about 105 bytes of JSON, so one image is
eight rows or roughly 880 bytes and 5,000 images are about 4.4 MB. That leaves the same headroom
the video cap leaves (10,800 positions ≈ 12.5 MB of a 16 MiB budget) and is comfortably below the
CLI's `MAX_COLLECTION_IMAGES` = 20,000, which stays what it is: the operator's local import has no
HTTP body and no browser to run out of patience in. A set is a shoot, not an archive.

**No feature flag.** A flag is for a change that could alter what an existing client does; this one
only *widens* what is accepted. A video document takes the identical path it took before — the same
function, the same checks, the same tag — and no stored row changes meaning. What is new is one
`kind` the endpoint used to refuse and one screen mode; a flag around either would only mean the
screen offering something the server then refuses. The rights gate, the duplicate rules and the
production write path are untouched.

### 4. The rights declaration becomes kind-neutral: version `2026-09-18`

The declaration a customer ticks says "the rights in this video" (`rights_confirmation.json`,
version `2026-09-17`). For a photo set that sentence is false, and a declaration is the one string
in this product that must be exactly true. Following the rule the file already states — **a new
wording is a new version, never an edit** — version `2026-09-18` says *original*, the word the
product uses for the thing being added, in both languages. Old confirmations keep pointing at the
sentence they were given; `TEXTS` still holds it.

One current version for both kinds was preferred over a version per kind: `RightsConfirmation`
records the exact sentence a person ticked, and a second dimension on that record buys nothing a
truthful single sentence does not already give.

### 5. What the customer sees, and what stays out of reach

- **The API says which kind an original is.** `CustomerOriginalOut.kind` is `video` or `photos`.
  It could have been inferred from a null `duration_s`, which is what a photo collection has — and
  that is exactly the kind of inference that is right until it is quietly wrong. The kind is a fact;
  it is sent as one.
- **The screens read truthfully.** "Add an original" asks first what is being added and then shows
  a photo set's own words: many files, "your photos stay on this computer", progress per image, the
  count of images that were skipped and why. The originals list, the detail and the record speak of
  a photo set where the words are about the shape of the work.
- **Extent.** A photo collection has no timeline and no duration, so `copy_extent` classifies every
  photo match `unknown` → `not_determined`, whose sentence today is "The preview images do not show
  how much of your video is on this page." Photos get their own truthful sentence
  (`explanationPhotos`), and only that one: `full_copy` and `excerpt` cannot occur for a
  collection, so no other extent word needs a variant, and the overview's words cost every visitor.
- **No image, still.** Not the pirated copy, not a page thumbnail, and not the customer's own
  photos. See §2.
- **Notice drafts get a photo evidence sentence, marked as a draft** (§6).

### 6. The notice wording for photographs is a drafted variant, not a decision

`marceline/notices/renderers.py` states content identity as a technical finding, and it states it
in terms of **audiovisual material**: "the audiovisual material available at … was compared with the
registered reference work …", "Marceline's perceptual-comparison system identified matching
audiovisual content". For a photo set that is wrong on its face, and a notice whose evidence
paragraph misdescribes the work is worse than no notice.

`NoticeContext` therefore carries `work_kind` and the evidence paragraph has a photograph variant in
both languages, following the same structure and the same limits as the video one (a technical
finding, no image, no hash, no band, no frame count). The annex's covering sentence follows it. The
variant is written into the code as **a draft**, marked in the module and listed in
the issue; the video wording is untouched. The machinery renders, and a human reads before anything is sent —
which is the only way anything is ever sent here anyway (ADR 0035: Marceline transmits nothing).

The sentences are listed verbatim in the pull request and in the issue, so the review does not have
to start by reading the diff.

## What this will and will not find today — the honest part

A photo set matches when a page we crawled carries **those photographs as images we hashed**. The
matcher needs no change for that: `find_duplicates`, the Hamming index and `match_references`
compare hashes and do not care which shape of original produced one, and ADR 0020 already decided
how a collection's evidence is counted (independent image pairs, three of them for the high band).

What the *index* holds is another question:

- Today's crawl is video-shaped. The adapters index video pages and the media lane fetches their
  **posters and hover frames** — one to ten images per page. Those are real images and a leaked
  photograph republished as a video page's poster would be found. That is a narrow target.
- A reposted photo **gallery** — the usual shape of a leaked set — is a page whose media are the
  photographs themselves. Nothing in the index is wrong about that; there is simply no adapter
  pointed at such a site yet, and `deactivate_untargeted` keeps the index to pages about target
  creators, so coverage follows the sources work rather than this change.
- Where a photo leak is *reposted on a video site's gallery section*, today's adapters index the
  detail page and fetch what the adapter declares as media. Whether that is the gallery's images is
  per site, and per adapter, and is answered by looking, not by asserting.

So: **this change makes photo originals addable and comparable; it does not by itself make photo
leaks findable at scale.** That depends on which sources are crawled and what their adapters
declare as media — the "where creator content actually leaks" research running in parallel
(2026-09-18), which this ADR deliberately does not duplicate or pre-empt. The UI says nothing more
than the video flow says: originals are compared with the sources we watch, and matching copies
reviewed by a person appear in the account. No promise about photographs specifically, because
none can be kept yet.

## Consequences

- **No migration.** `Reference.kind` and the `reference_shape_matches_kind` constraint have existed
  since ADR 0020, and `Reference.pipeline` is a plain `CharField` with no choices, so a new tag is
  data, not schema. Nothing to apply, nothing to restart for.
- **The OpenAPI schema changes** (`CustomerOriginalOut.kind`, the `too_many_images` code): `make
  openapi` and `make frontend-api` were re-run, and both generated files are in the change.
- `import_fingerprints --compare` will report a pipeline mismatch between a browser-hashed set and a
  CLI-hashed one of the same photographs. That is correct and is the point: they are two references
  of the same material on two recipes, exactly as ADR 0022 arranged for video.
- A browser photo reference and a CLI photo reference of the same folder have the **same collection
  digest** and different pipelines, so `reference_unique_sha256_pipeline_fps` lets both exist. A
  customer re-adding their own set answers 200 with the original they have, because the endpoint's
  idempotence is per organisation and per digest, whatever the recipe.
- Hashing a large set is slow: one decode and eight PDQ hashes per image, in one Worker. A 300-photo
  set is a minute or two with the tab open, and the screen says so. Throughput on a real creator
  laptop is unmeasured, as it is for video.
- **It costs the app's worst route 0.90 KB** (measured on the check runner, 2026-09-19):
  `/app/originals/new` goes from 146.90 to **147.80 KB** gzip first load, its own chunks from 12.37
  to **13.24 KB** of 16, so **2.20 KB of headroom** is left against the 150 KB ceiling. Everything
  the first load gains is the kind switch, the photo phase and the leaf format module; the builder,
  the digest and the hasher arrive with the client on start. In the Vite SPA the `/app` first load
  goes 142.87 → 143.13 KB and the core words 4.07 → 4.11 KB of 4.5, which is the single extent
  sentence §5 keeps.
- **The phone note now names the kind** (issue #117): "a video works best on a computer … a photo
  set is fine on a phone". That sentence was written the day before this change and was true only
  of video; a set is read one picture at a time and a photograph is small, so a creator who came on
  a phone to add a set is no longer sent to find a computer.
- The public site says "Videos" throughout. It is **not** edited here; the minimal wording change
  for when this goes live is proposed in the issue and in `website/README.md`'s "Before going live"
  neighbourhood, for the operator to make deliberately.

## Alternatives rejected

- **Reusing `pillow-rgb-pdq-v0` for browser-hashed sets.** It would make a browser reference and a
  CLI reference indistinguishable in the only field that records the difference, on the strength of
  an assumption about JPEG decoders that nobody has measured. ADR 0019 rejected the same move for
  video with a measurement in hand; here there is not even that.
- **One reference per photograph.** Settled by ADR 0020 and unchanged: a leak reposts the set, so
  the unit of review is the set.
- **Showing the customer their own photos back** (a thumbnail strip while hashing). It is their
  file, so the privacy objection is weaker — but the *code* objection is not: the app would then
  contain an element able to render an image, and the two tests that make "no picture, ever" a
  property rather than a promise would have to be relaxed to let it in. A count and a list of
  skipped files is enough to know the right files were picked.
- **A folder picker** (`webkitdirectory`). Non-standard, silently empty in some browsers, and it
  hands us the relative path of every file — which is exactly the thing this client must not send.
  Multiple selection and drag-and-drop of a folder's contents do the same job with no path.
- **A feature flag.** See §3.
