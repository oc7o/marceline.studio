# Vendored source: Meta's PDQ reference implementation (C++)

| | |
|---|---|
| Upstream | https://github.com/facebook/ThreatExchange |
| Commit | `d2d07c1841648085a7316852c34e75dd188d6ea4` (2026-09-11) |
| Path upstream | `pdq/cpp/` (until commit `2a306a8` this lived at `hashing/pdq/cpp/`) |
| Licence | BSD-3-Clause, `LICENSE` in this folder (copied from the repository root) |
| Copied on | 2026-09-16 |

## What was copied

Only the files needed to compute a PDQ hash and its 8 dihedral variants, unmodified:

```
pdq/cpp/common/pdqbasetypes.h        Hash8/16/64 aliases
pdq/cpp/common/pdqhamming.h          popcount helpers (header only; pdqhashtypes.h includes it)
pdq/cpp/common/pdqhashtypes.h        Hash256 (the 256-bit hash, bit accessors)
pdq/cpp/downscaling/downscaling.h    Jarosz box filter + decimation
pdq/cpp/downscaling/downscaling.cpp
pdq/cpp/hashing/pdqhashing.h         pdqDihedralHash256esFromFloatLuma and friends
pdq/cpp/hashing/pdqhashing.cpp
pdq/cpp/hashing/torben.h             median of the 16x16 DCT output
pdq/cpp/hashing/torben.cpp
```

Deliberately **not** copied: `pdqhashtypes.cpp` (hex parsing, `format()`, `fuzz()` - needs
`<random>` and `sscanf`, and nothing in the hashing path calls it), `pdqhamming.cpp` (900 KB of
lookup tables, unused because `pdqhamming.h` defines `USE_BUILTIN_POPCOUNT`), the `bin/`,
`index/`, `io/` and `reg_test/` trees, `CImg.h` and the Makefile.

No local modifications. The directory layout is kept as `pdq/cpp/...` because the sources include
each other as `<pdq/cpp/common/pdqhashtypes.h>`; `build.sh` passes `-I vendor`.

## Why this commit matches the server

The server hashes with the `pdqhash` PyPI package (0.2.8), which vendors its own copy of these
sources. Diffing pdqhash 0.2.8's copy against this commit shows **only cosmetic differences** in
the hashing path:

- `pdqhashing.cpp`: `M_PI` replaced by a local `constexpr double pi` with the same value, plus
  moved `#include`s.
- `pdqhashing.h` / `downscaling.h`: `using uint8_t = unsigned char;` replaced by `<cstdint>`.

`downscaling.cpp`, `torben.cpp`, `pdqhashtypes.h`, `pdqhamming.h` and `pdqbasetypes.h` are
byte-identical. No arithmetic differs, which is why the WASM build reproduces the server's hashes
bit for bit (see `backend/matching/tests/fixtures/pdq/` and the parity tests).

## Refreshing

Re-copy from a newer commit only together with a re-run of the parity tests
(`backend/matching/tests/test_pdq_parity.py` and `frontend/src/lib/pdq.test.ts`). A change in the
hashing math would make new browser hashes incomparable with the stored server ones and needs a
new pipeline tag, not a silent update.
