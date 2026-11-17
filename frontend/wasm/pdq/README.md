# PDQ hashing in the browser (WebAssembly)

Meta's PDQ hasher (C++, BSD-3) compiled to WebAssembly so the fingerprinting client can hash
frames locally and upload **only hashes**. The build output is committed, so `npm ci` users never
need Docker; only someone changing the hasher rebuilds it.

| | |
|---|---|
| Source | `vendor/pdq/cpp` (ThreatExchange commit + licence in `vendor/SOURCE.md`) |
| Glue | `src/pdq_wasm.cpp` (two C entry points, see below) |
| Toolchain | `emscripten/emsdk:4.0.9`, pinned in `build.sh`, run through Docker |
| Output | `../../src/wasm/pdq-wasm.js` (single file: the wasm is embedded, `-s SINGLE_FILE=1`) |
| Types | `../../src/wasm/pdq-wasm.d.ts` (hand-written declarations for the generated module) |
| Wrapper | `../../src/lib/pdq.ts` (`hashDihedral`, `hashDihedralLuma`, hex helpers) |

## Rebuilding

```bash
./frontend/wasm/pdq/build.sh      # from the repo root; needs Docker, nothing else
```

The script pulls the pinned image, compiles, prepends a `@generated` banner and prints the size
and sha256 of the artifact. It is re-runnable and reproducible: the same sources and the same
image produce byte-identical output (verified by building twice and comparing).

After **any** rebuild, re-run both parity tests - they are the only thing that proves browser
hashes are still comparable with the server's:

```bash
cd backend  && POSTGRES_DB=marceline_browser uv run pytest matching/tests/test_pdq_parity.py
cd frontend && npx vitest run src/lib/pdq.test.ts
```

Bumping the image tag is a deliberate change, not housekeeping: a different Emscripten/LLVM can
generate different floating-point code and move hash bits.

## Exported API

Both take a pointer to pixel bytes and write `8 * 32` bytes (the eight dihedral variants, in
`pdqhash.compute_dihedral` order: original, rot90, rot180, rot270, flipX, flipY, flipPlus1,
flipMinus1). They return the PDQ quality metric 0..100, or -1 for invalid dimensions.

```c
int marceline_pdq_dihedral_rgb (const uint8_t* rgb,  int numRows, int numCols, uint8_t* out);
int marceline_pdq_dihedral_luma(const uint8_t* grey, int numRows, int numCols, uint8_t* out);
int marceline_pdq_abi_version(void);
```

`_malloc` / `_free` and `HEAPU8` are exported so the wrapper can move buffers in and out.

Hash bytes are the canonical ThreatExchange order (word 15 first, big-endian within each word),
so `toHex()` produces exactly the 64-character string the backend stores and prints.

## Why `-ffp-contract=off`

PDQ thresholds each of 256 DCT coefficients against their median. Coefficients near that median
decide a bit on a difference of a few ULP, so *any* change in float rounding flips hash bits.

By default both Clang and GCC may contract `a * b + c` into a single fused multiply-add, which
rounds once instead of twice. PDQ's box filters and its 64x64 -> 16x16 DCT are exactly such long
multiply-accumulate chains. The server's `pdqhash` is built without contraction (plain
`--std=c++11`, no `-ffast-math`), so the WASM build passes `-ffp-contract=off` and
`-fno-fast-math` to get the same arithmetic. Without it the two implementations agree on most
frames and differ on a few bits for others - the worst possible failure mode, because it looks
like a matching problem rather than a build problem.

The same reasoning rules out `-ffast-math` and any reassociation flags. `-O3` is safe: it does
not change IEEE semantics on its own.

## Matching the Python binding exactly

`pdqhash.compute_dihedral` does **not** use Meta's `fillFloatLumaFromRGB`. Its Cython glue
computes the luma in numpy:

```python
gray = (image[:, :, 0] * 0.299 + image[:, :, 1] * 0.587 + image[:, :, 2] * 0.114).astype("float32")
```

Every product and sum happens in **float64**, with a single rounding to float32 at the end.
Meta's C++ helper multiplies `float` coefficients in float precision, which rounds differently.
`marceline_pdq_dihedral_rgb` therefore reproduces the numpy form (double precision, left to
right, one final cast) rather than calling the C++ helper. This is the second thing parity
depends on, next to the compiler flags.

`marceline_pdq_dihedral_luma` takes a single 8-bit plane (R=G=B=Y) and widens it exactly, for the
canonical-pipeline path in `docs/research/fingerprinting-client.md`. It is not the recipe today's
server references use.

## Licence

Meta's PDQ sources under `vendor/` are BSD-3-Clause; the licence text is `vendor/LICENSE` and the
provenance is `vendor/SOURCE.md`. `src/pdq_wasm.cpp` and `build.sh` are part of Marceline.
