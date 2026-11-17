# PDQ reference vectors from Meta's ThreatExchange

Fixtures for `matching/tests/test_pdq_threatexchange.py` (issue #6): our PDQ hashes checked against
the hashes Meta publishes for these exact files, so the bit order, the hex format and the dihedral
variant order are verified against the reference implementation instead of against ourselves.

## Upstream

- Repository: <https://github.com/facebook/ThreatExchange>
- Commit: `d2d07c1841648085a7316852c34e75dd188d6ea4` (2026-09-11)
- Licence: BSD, Copyright (c) Meta Platforms, Inc. and affiliates — the upstream `LICENSE` file is
  copied next to these images.

These are Meta's own BSD-licensed test images (a photo of a bridge, a colour chart, a screenshot of
text). They are sample data for a hash test, not crawled or third-party media, so they are not
covered by the project's "never store media" rule — nothing here comes from a site we index.

| File | Upstream path | Bytes |
|---|---|---|
| `bridge-1-original.jpg` | `pdq/data/reg-test-input/dih/bridge-1-original.jpg` | 361 182 |
| `rgb.jpeg` | `python-threatexchange/threatexchange/tests/hashing/resources/rgb.jpeg` | 40 022 |
| `LA.png` | `python-threatexchange/threatexchange/tests/hashing/resources/LA.png` | 131 545 |
| `I16.png` | `python-threatexchange/threatexchange/tests/hashing/resources/I16.png` | 131 545 |
| `random-screenshot.png` | `RANDOM_IMAGE_BASE64` in `python-threatexchange/threatexchange/tests/hashing/test_pdq_hasher.py`, base64-decoded | 2 097 |

`vectors.json` holds the expected hashes, copied verbatim from upstream:

- `photo_hasher` and `dihedral`: the `hash=…,quality=…` and `xform=…` lines of
  `pdq/cpp/reg_test/expected/out`, produced by Meta's C++ `pdq-photo-hasher`
  (`--details` and `--pdqdih --details`).
- `transformed_files`: the per-file hashes of all eight `dih/bridge-*.jpg` from the same expected
  output. Those seven sibling images are **not** committed (≈360 kB each); only their published
  hashes are, which is enough to pin our dihedral ordering.
- `pillow_pdqhash`: the expected hashes in `test_pdq_hasher.py`, produced by Pillow + the `pdqhash`
  package — the same stack `matching.pdq` uses.

Nothing is downloaded at test time; refresh by hand from the URLs above if upstream ever changes.

## Measured distances (2026-09-16, Pillow 12.3.0, numpy 2.5.3, pdqhash from `backend/uv.lock`)

| Vector group | Hamming distance to our hash |
|---|---|
| `pillow_pdqhash` (4 images) | **0** for every image, quality identical |
| C++ `pdq-photo-hasher`, `bridge-1-original.jpg` | 6 (quality 100 on both sides) |
| C++ `--pdqdih`, the 8 orientations of that image | 6, 8, 4, 4, 4, 8, 6, 4 |
| our dihedral variant vs the published hash of the separately transformed file | 6, 6, 14, 10, 10, 6, 6, 14 — and the matching orientation is always the nearest of the eight, the runner-up being 104–108 bits away |

The non-zero distances in the C++ rows are decoder differences, not hashing differences: Meta's C++
tool decodes the JPEG with CImg/libjpeg and builds float32 luma per pixel, while we decode with
Pillow and let `pdqhash` compute luma in numpy float64 before casting to float32. The identical
`pillow_pdqhash` row shows the hashing itself is bit-exact when both sides start from the same
pixels. The test's tolerances are documented in its module docstring.
