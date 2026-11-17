# 0017 — PDQ in the browser: vendored C++ compiled to WebAssembly

Status: accepted (2026-09-16)

## Context

The fingerprinting client (issue #29, `docs/research/fingerprinting-client.md`) hashes a creator's
original in their browser and uploads only hashes. Those hashes are compared against reference and
page hashes produced on the server by `pdqhash.compute_dihedral` at a Hamming threshold of 31
(`matching/fingerprint.py`). If the browser's hasher disagrees with the server's by even a few
bits per frame, nothing fails loudly: matches just get weaker, and the cause looks like a matching
problem rather than a build problem.

The research doc proposed compiling Meta's `hashing/pdq/cpp` ourselves and exporting
`pdqDihedralHash256esFromFloatLuma`, rather than trusting the unmaintained `pdq-wasm` npm package.
Building it surfaced two details that the plan did not anticipate, both of which change the
exported shape.

## Decision

**Vendor Meta's PDQ C++ and compile it with a pinned Emscripten image.** Sources sit in
`frontend/wasm/pdq/vendor/pdq/cpp` (ThreatExchange `d2d07c1`, BSD-3, provenance in
`vendor/SOURCE.md`), unmodified. `frontend/wasm/pdq/build.sh` runs `emscripten/emsdk:4.0.9` in
Docker and writes a single self-contained ES module to `frontend/src/wasm/pdq-wasm.js`
(`-s SINGLE_FILE=1`, wasm embedded). That artifact is **committed**, so `npm ci` users need no
toolchain; only someone changing the hasher rebuilds. The build is reproducible (built twice,
byte-identical).

**Export a small C ABI, not Meta's float-luma function.** `src/pdq_wasm.cpp` exports:

```c
int marceline_pdq_dihedral_rgb (const uint8_t* rgb,  int numRows, int numCols, uint8_t* out);
int marceline_pdq_dihedral_luma(const uint8_t* grey, int numRows, int numCols, uint8_t* out);
int marceline_pdq_abi_version(void);
```

Each writes 8 × 32 bytes in `compute_dihedral` order (original, rot90, rot180, rot270, flipX,
flipY, flipPlus1, flipMinus1 — the numbering `ReferenceFrameHash.dihedral` already uses) and
returns the quality metric. Taking pixels rather than a float luma buffer keeps the luma
conversion — the part that has to match Python exactly — inside the module, on the C++ side of the
boundary, instead of in JavaScript where `Math` would round differently.

**Reproduce the binding's luma conversion, not Meta's.** `pdqhash.compute_dihedral` does not call
`fillFloatLumaFromRGB`; its Cython glue computes
`(R*0.299 + G*0.587 + B*0.114).astype("float32")` in numpy, i.e. in **float64** with a single
rounding to float32 at the end. Meta's C++ helper multiplies `float` coefficients in float
precision and rounds differently. `marceline_pdq_dihedral_rgb` therefore implements the numpy
form. `marceline_pdq_dihedral_luma` widens a single 8-bit plane exactly, for the canonical
(Y-plane) pipeline the research doc proposes.

**Compile with `-ffp-contract=off` and `-fno-fast-math`, at `-O3`.** PDQ thresholds 256 DCT
coefficients against their median, so coefficients near the median decide a bit on a few ULP. A
fused multiply-add in the box filters or the DCT rounds once instead of twice and flips bits. The
server's `pdqhash` is built without contraction, so the WASM build must be too. Bumping the
Emscripten tag is a deliberate change for the same reason.

**Parity is enforced by fixtures that exist exactly once.**
`backend/matching/tests/generate_pdq_fixtures.py` (run manually) writes 14 small synthetic RGB
images plus today's Python hashes to `backend/matching/tests/fixtures/pdq/*.json`: gradients,
seeded noise, checkerboards, stripes, near-uniform low-quality frames, odd and non-square sizes,
the 5×5 minimum and the 64×64 the hasher downsamples to. `matching/tests/test_pdq_parity.py`
asserts Python still produces them; `frontend/src/lib/pdq.test.ts` reads the same files through
`import.meta.glob` and asserts the WASM build returns identical hex for all eight orientations and
an identical quality integer.

## Consequences

- Browser hashes are bit-identical to server hashes for identical pixels — verified, not assumed.
  Quality matches exactly too, including frames where quality is 0, so `MIN_QUALITY` gating
  behaves the same on both sides. No assertion had to be weakened.
- Remaining divergence risk in the client moves entirely to **frame choice and scaling** (stage B
  and the canonical pipeline), which is where the research doc already expected it.
- `frontend/src/wasm/` holds a generated binary-ish artifact in git (45 KB). eslint ignores it and
  tsc sees only the hand-written `pdq-wasm.d.ts`. The alternative — building in CI — would make
  every `npm ci` depend on Docker.
- Docker is now needed to *change* the hasher, never to run or test the app.
- We keep one more vendored dependency to track. `vendor/SOURCE.md` records the commit; a refresh
  is only allowed together with a re-run of both parity tests, and a change in hashing math would
  need a new pipeline tag rather than a silent update.

## Alternatives rejected

- **`pdq-wasm` from npm**: 0 stars, single maintainer, v0.1.0, no dihedral API and no claim of
  test-vector parity. Trusting it would put the hash contract outside our control.
- **Reimplementing PDQ in TypeScript or WASM by hand**: guarantees a second, subtly different
  implementation to keep in sync forever.
- **Calling Meta's `fillFloatLumaFromRGB`**: cleaner-looking, but provably not what the server
  does; it was the first thing tried and it does not match on every fixture.
- **Building the wasm in CI instead of committing it**: makes a normal `npm ci` checkout depend on
  a 1 GB toolchain image for a file that changes a few times a year.
