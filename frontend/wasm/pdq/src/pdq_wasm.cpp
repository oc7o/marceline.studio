// ================================================================
// Marceline's C entry points around Meta's PDQ hasher (vendor/pdq/cpp).
//
// The server hashes reference frames with the `pdqhash` Python binding
// (matching/fingerprint.py -> pdqhash.compute_dihedral). The browser must
// produce the same 32-byte hashes for the same pixels, so these wrappers
// reproduce that binding's behaviour exactly:
//
//  * Luma. `pdqhash`'s Cython glue does NOT call Meta's fillFloatLumaFromRGB.
//    It computes, in numpy:
//        (R*0.299 + G*0.587 + B*0.114).astype('float32')
//    i.e. every product and sum in *double* precision, rounded to float32 once
//    at the end, left to right. Meta's C++ helper instead multiplies float
//    coefficients in float precision, which rounds differently and can flip
//    hash bits. `dihedral_rgb` therefore does the double-precision form.
//    `dihedral_luma` (stage B: hash the Y plane directly) uses Meta's grey
//    path, a plain widening uint8 -> float, which is exact.
//
//  * Bit order. `pdqhash` reads bit k as (w[(k & 255) >> 4] >> (k & 15)) & 1
//    for k = 255..0 and packs those bits MSB-first into 32 bytes, which is the
//    canonical ThreatExchange hex string: word 15 first, high byte first.
//
//  * Dihedral order: original, rot90, rot180, rot270, flipX, flipY,
//    flipPlus1, flipMinus1 - the order compute_dihedral returns and the order
//    `Reference`/`ReferenceFrameHash.dihedral` numbers 0..7.
//
// Build flags matter as much as the code: see build.sh (-ffp-contract=off).
// ================================================================

#include <pdq/cpp/common/pdqhashtypes.h>
#include <pdq/cpp/hashing/pdqhashing.h>

#include <emscripten/emscripten.h>

#include <cstddef>
#include <cstdint>
#include <vector>

using facebook::pdq::hashing::Hash256;

namespace {

constexpr int kNumOrientations = 8;
constexpr int kHashBytes = 32;

// Canonical PDQ byte order: word 15 down to word 0, each big-endian.
void writeHash(const Hash256& hash, uint8_t* out) {
  for (int i = 0; i < 16; i++) {
    const uint16_t word = hash.w[15 - i];
    out[2 * i] = static_cast<uint8_t>(word >> 8);
    out[2 * i + 1] = static_cast<uint8_t>(word & 0xff);
  }
}

// `luma` is consumed (the Jarosz filter works in place); `out` takes 8*32 bytes.
int dihedralFromLuma(float* luma, int numRows, int numCols, uint8_t* out) {
  std::vector<float> scratch(static_cast<size_t>(numRows) * numCols, 0.0f);

  float buffer64x64[64][64];
  float buffer16x64[16][64];
  float buffer16x16[16][16];
  float buffer16x16Aux[16][16];
  Hash256 hashes[kNumOrientations];
  int quality = 0;

  facebook::pdq::hashing::pdqDihedralHash256esFromFloatLuma(
      luma,
      scratch.data(),
      numRows,
      numCols,
      buffer64x64,
      buffer16x64,
      buffer16x16,
      buffer16x16Aux,
      &hashes[0],
      &hashes[1],
      &hashes[2],
      &hashes[3],
      &hashes[4],
      &hashes[5],
      &hashes[6],
      &hashes[7],
      quality);

  for (int i = 0; i < kNumOrientations; i++) {
    writeHash(hashes[i], out + i * kHashBytes);
  }
  return quality;
}

} // namespace

extern "C" {

// Packed RGB24, numRows*numCols*3 bytes. Writes 8*32 bytes to `out`.
// Returns the PDQ quality metric (0..100), or -1 for invalid dimensions.
EMSCRIPTEN_KEEPALIVE
int marceline_pdq_dihedral_rgb(
    const uint8_t* rgb,
    int numRows,
    int numCols,
    uint8_t* out) {
  if (numRows <= 0 || numCols <= 0) {
    return -1;
  }
  const size_t count = static_cast<size_t>(numRows) * numCols;
  std::vector<float> luma(count);
  for (size_t i = 0; i < count; i++) {
    // Double precision throughout, one rounding to float at the end: this is
    // what numpy does inside pdqhash.compute_dihedral.
    const double value = static_cast<double>(rgb[3 * i]) * 0.299 +
        static_cast<double>(rgb[3 * i + 1]) * 0.587 +
        static_cast<double>(rgb[3 * i + 2]) * 0.114;
    luma[i] = static_cast<float>(value);
  }
  return dihedralFromLuma(luma.data(), numRows, numCols, out);
}

// Single-plane 8-bit luma (Y), numRows*numCols bytes. Writes 8*32 bytes.
// Returns the PDQ quality metric (0..100), or -1 for invalid dimensions.
EMSCRIPTEN_KEEPALIVE
int marceline_pdq_dihedral_luma(
    const uint8_t* grey,
    int numRows,
    int numCols,
    uint8_t* out) {
  if (numRows <= 0 || numCols <= 0) {
    return -1;
  }
  const size_t count = static_cast<size_t>(numRows) * numCols;
  std::vector<float> luma(count);
  for (size_t i = 0; i < count; i++) {
    luma[i] = static_cast<float>(grey[i]);
  }
  return dihedralFromLuma(luma.data(), numRows, numCols, out);
}

// Lets callers assert they load the module they expect.
EMSCRIPTEN_KEEPALIVE
int marceline_pdq_abi_version() {
  return 1;
}

} // extern "C"
