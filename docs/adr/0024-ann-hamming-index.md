# 0024 — Approximate nearest neighbour index for matching

Status: accepted (2026-09-16)

## Context

A full `match_references` run compares 110,706 page frames against 39 references (~432,000
quality-eligible reference hashes, 1 fps × 8 dihedral variants). It takes **1,952 s** and has been
OOM-killed once on the 8 GB box. Both sides grow without bound: page frames with every site
crawled, references with every customer onboarded. The brute-force scan is O(page frames ×
reference hashes) and is the wall.

ADR 0015 already predicted this: "past a few hundred thousand frames it needs an ANN/FAISS index
(ADR needed)". This is that ADR.

The operator approved the work on 2026-09-16 with "minimal quality loss compared to the compute
time it saves us". An approximate index can miss a true near-match, so the exchange rate had to be
measured, not assumed.

## Decision

**Add FAISS binary indexes behind `matching.hamming.HashIndex`, and make the exact one the
default. We are not trading any quality for the speedup.**

`faiss-cpu` 1.15.0 installs cleanly on Python 3.13 with `uv` (a prebuilt wheel, no compilation),
so no alternative was needed.

Three backends, chosen by `matching.ann.resolve_backend`:

| backend | what | when |
|---|---|---|
| `numpy` | the original XOR/popcount scan | below `MIN_FAISS_SIZE` (4096 hashes), and whenever `faiss` is missing |
| `flat` | `IndexBinaryFlat`, **exact** | the default above 4096 hashes |
| `ivf` | `IndexBinaryIVF`, approximate | opt-in only, never automatic |

`HashIndex` keeps its own (N, 4) uint64 copy of every hash, so FAISS is only ever a *candidate
generator*: every candidate is re-ranked with the exact distance before it is returned. A reported
distance is therefore never an approximation, and a returned pair is never above the threshold.
The interface is unchanged, so matching, common frames, duplicates and clustering keep working
untouched; `backend=` and the `MATCHING_ANN_BACKEND` setting are the only additions, plus
`add(ids, hashes)` for incremental use.

FAISS binary `range_search` is exclusive (`d < radius`), so every call passes `max_distance + 1`.
Queries are searched in batches of `QUERY_BATCH` (8192): `range_search` materialises every hit of
a batch in one result, and an unbatched 110k-query scan is exactly the shape that got the run
OOM-killed.

### Why not the approximate ones

`IndexBinaryHNSW` **has no `range_search` at all** (FAISS raises "range search not implemented").
Our entire matching API is radius-based, not k-NN, so HNSW can only be emulated with a k-nearest
search and an arbitrary `k` cap — which silently truncates exactly the dense neighbourhoods
(a leak reposted many times) where recall matters most.

`IndexBinaryIVF` supports `range_search`, but at our scale it loses on **both** axes: it is lossy
*and* slower end to end, because its k-means training costs more than the exact scan it replaces
(112 s to train nlist 1024 over 432k hashes, against 83 s for the whole exact run).

**Exact re-ranking does not make an approximate backend lossless.** The measurements below show
zero false positives for every configuration, so re-ranking has nothing to remove: it cannot
recover a neighbour the index never returned. That is precisely why the exact backend is the
default, and it is the best available outcome — most of the speedup, none of the loss.

## Measurements

Benchmarks are reproducible: `matching/management/commands/benchmark_ann.py`. Synthetic images and
hashes only, never real or pirated media.

### The corpus is realistic, and here is where it is not

Unrelated **real** PDQ hashes (719,400 pairs from 1,200 distinct synthetic images) are distributed
almost exactly like uniform random 256-bit strings — mean distance 128.0 (σ 8.29) against 128.0
(σ 8.01), minimum 84 against 91. **No unrelated pair anywhere falls within 40**, which is why every
configuration below reports zero false positives: the whole recall question is whether the index
finds the planted near-duplicates, not whether it drowns in noise.

The near-duplicate distances are measured from real transforms of synthetic images: jpeg q70 0–4,
jpeg q40 0–8, resize to 320 px 4–24, resize to 160 px 6–30, 1% crop 10–42, 3% crop 34–82,
brightness ±20% 0–28. The 8 dihedral variants sit at mean distance 126 from the original, i.e.
they behave as independent points — so a reference really does cost 8× its frame count.

Planted leak pairs (the recall denominator) are produced by those real transforms. The bulk filler
is modelled: random bit flips around real base hashes, standing in for consecutive video frames
and reposts. Real near-duplicates move in a correlated bit subspace and random flips do not; this
is the one concession to tractability, and it makes the clusters *harder* to partition cleanly, so
it does not flatter the exact backend.

### Recall (100,000 index hashes, 20,000 queries, 11,835 true pairs at 31 / 11,994 at 40)

`query recall` is the fraction of the 2,981 queries with a true neighbour that found at least one
of them — the metric that actually decides whether a leak is missed, since `scan_candidates` needs
only one hit per (page frame, reference) to create the candidate all evidence is rebuilt from.

| index | build s | qps | recall@31 | recall@40 | query recall@31 | missed pairs@31 | missed queries@31 |
|---|---|---|---|---|---|---|---|
| **numpy (exact)** | 0.08 | 203 | **1.000000** | **1.000000** | **1.000000** | 0 | 0 |
| **flat (exact)** | 0.00 | **8,481** | **1.000000** | **1.000000** | **1.000000** | **0** | **0** |
| ivf nlist 256, nprobe 1 | 5.44 | high | 0.601352 | 0.597716 | 0.746058 | 4,718 | 757 |
| ivf nlist 256, nprobe 8 | 3.27 | high | 0.939755 | 0.936468 | 0.976182 | 713 | 71 |
| ivf nlist 1024, nprobe 1 | 18.37 | high | 0.585889 | 0.581791 | 0.727273 | 4,901 | 813 |
| ivf nlist 1024, nprobe 8 | 41.31 | 2,955 | 0.911534 | 0.907871 | 0.958739 | 1,047 | 123 |
| ivf nlist 1024, nprobe 32 | 26.71 | 75,722 | 0.987157 | 0.985159 | 0.997316 | 152 | 8 |
| ivf nlist 4096, nprobe 16 | 70.45 | 11,531 | 0.990706 | 0.989828 | 0.993626 | 110 | 19 |
| ivf nlist 4096, nprobe 64 | 114.57 | 2,633 | 0.998648 | 0.998499 | 0.998994 | 16 | 3 |
| hnsw M 32, efSearch 64, k 64 | 5.66 | 24,545 | 0.944064 | 0.941221 | 0.952030 | 662 | 143 |
| hnsw M 32, efSearch 256, k 64 | 5.82 | 6,983 | 0.994339 | 0.994414 | 0.998323 | 67 | 5 |

False positives at 40: **0 for every row**, before re-ranking.

**The miss rate we are declining to accept:** the best approximate configuration (ivf 4096/64)
still loses 16 true pairs and 3 leak-bearing queries per 20,000, after 115 s of training. The best
HNSW configuration loses 67 pairs and 5 queries, and is *slower* than exact flat (6,983 vs 8,481
qps). Every approximate row is dominated by the exact row.

The IVF qps column is noisy (single runs, OpenMP thread contention); the recall and build-time
columns are what the decision rests on, and those are unambiguous.

### Speed and memory at match-run scale (432,000 index hashes)

Per-query cost timed over a sample, projected to the full 110,706-query run:

| backend | build s | ms/query | projected search | total incl. build | peak RSS |
|---|---|---|---|---|---|
| numpy (exact) | 0.14 | 22.47 | 2,487 s (41 min) | 2,487 s | 201.6 MB |
| **flat (exact)** | **0.01** | **0.747** | **82.7 s** | **83 s** | **76.1 MB** |
| ivf 1024/32 | 112.41 | 0.013 | 1.4 s | 114 s | 132.9 MB |
| ivf 4096/32 | 278.78 | 0.031 | 3.4 s | 282 s | 133.6 MB |

Exact flat is **~30× faster than the numpy scan and still exact**, and it is faster end to end than
either approximate index once training is counted.

That projection turns out to be conservative. Running the **whole** scan for real — all 110,706
queries against 432,000 reference hashes on the realistic corpus — takes **38.9 s at a peak RSS of
94.5 MB** with the exact flat backend, against the **1,952 s** the operator measured in production.

**Before and after for a 110k × 39-reference run: 1,952 s → 39 s, with identical results.**

The "before" half of that line is the operator's production measurement plus the 2,000-query
sample above, not a full run measured here. The attempt to run the whole 110,706-query numpy scan
on this box was **killed for low memory** partway through, while a crawl and a match run were
live. It was not retried: re-running the exact workload this ADR exists to remove, on the 8 GB box
it already OOM-killed once, is not worth a confirmatory number. That failure is weak corroboration
of the premise rather than a measurement — the box could not finish the scan the new default
completes in 39 s inside 94.5 MB.

### Incremental index

The index does not have to be rebuilt from scratch. `HashIndex.add(ids, hashes)` appends to a
built FAISS index (and an IVF index keeps the centroids it was trained on), so
`matching.incremental` can extend the candidate scan rather than rebuild it. In practice the flat
build is a 14 MB memcpy (0.01 s at 432k hashes), so a rebuild is nearly free either way — the
expensive thing was never construction, it was the scan.

## Consequences

- `faiss-cpu` is a backend dependency. It ships prebuilt wheels for Python 3.13, so it does not add
  to the `pdqhash` compile-from-source burden. It gets a narrow mypy `ignore_missing_imports`
  override, like `pdqhash`.
- **A full match run goes from ~1,952 s to ~39 s of scan, with identical results.** The 39 s and
  its 94.5 MB peak are a real full run. The numpy side of that comparison is *not*: 201.6 MB is
  the peak of a 2,000-query sample against the same 432k index (see the note under the table).
  The direction is not in doubt — the numpy path materialises a (queries × hashes × words)
  temporary while FAISS streams a bounded result buffer — but treat the numpy peak as a sample,
  not as a measured full run.
- Matching results do not change at all, so no rescan or re-review is triggered and the ADR 0015
  settings fingerprint is untouched (the backend is deliberately *not* part of it: switching
  engines cannot change an answer).
- `build_common_frames`' O(frames²) self-join gets the same speedup through `neighbor_pairs`.
- If the index grows to where even exact flat is too slow, the approximate backend is already
  wired and measured: turn on `MATCHING_ANN_BACKEND=ivf`, accept the recall in the table above,
  and re-measure at the new scale. Revisit at ~10× the current frame count.
- FAISS uses OpenMP and will take all cores; on a shared box, cap it with `OMP_NUM_THREADS`.
