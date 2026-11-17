# 0010 — Creator references

Status: accepted (2026-09-15)

## Context

Page-to-page duplicates do not answer whether indexed pages contain a creator's original.
Creator originals are supplied locally, outside the repository. Dense sampling is needed because
sites choose different preview frames.

## Decision

`add_reference` reads a local original in place: store only metadata, its unique SHA256, and PDQ
fingerprints. Never copy or persist the file, decoded frames, or its path. The optional source
note must contain no personal data. Resolve creators by exact, case-insensitive name or alias;
ambiguous and unknown names fail. Imports are atomic, duplicate files fail, and a second digest
check rejects files changed during import. The operator controls retention of their source file.

Sample the whole video at the requested positive finite fps (default 1), streaming ffmpeg PPM
frames through a pipe and inserting hashes in bounded batches. Reuse the preview decoder's
512-pixel maximum width; do not apply its 64-frame cap. Only local input protocols are enabled.
A one-hour watchdog bounds decoding. Store ffprobe duration and sampling-grid timestamps
`frame_index / fps`, relative to video start (approximate sample times, not original packet PTS).
Skip frames below MIN_QUALITY. By default, `pdqhash.compute_dihedral` supplies eight orientations,
numbered in the binding's order with 0 original; `--no-dihedral` stores only the original hash.
No crop variants are included.

`match_references` builds one in-memory HashIndex over selected reference hashes and searches
all sites' page hashes. Both sides obey the minimum quality. Threshold comparison is inclusive.
Each page FrameHash contributes at most once per reference, regardless of how many reference
frames or orientations match. `page_frames` counts quality-eligible page FrameHashes;
`matched_frames` counts those within threshold. Best evidence is the lowest distance, breaking
ties with the earliest reference sampling timestamp. One matching frame proposes a page match.
This is candidate evidence, not proof of identity.

### Shared frames and match strength (2026-09-15)

Real originals share footage: 6 of 8 carried the same distributor intro (~0–13 s) and outro
(last ~7 s), 91 of 21,018 original-orientation frames at pairwise distance 0–20 and PDQ quality
~100. A quality filter cannot remove them, and each such frame matches every leak from the same
distributor. `ReferenceFrameHash.shared` marks them; `mark_shared_frames()` recomputes the flag
for all references (idempotent, called by `add_reference` and at the start of
`match_references` with the run's threshold and minimum quality). A frame position is shared when
- its dihedral-0 hash lies within the threshold of another reference's dihedral-0 hash, including
  references of the same creator (a common intro, not a leak), or
- any of its orientations matches page frames from at least `COMMON_PAGE_GROUPS` (5) distinct
  page groups of other creators; an active cluster is one group, an unclustered page is its own
  group. Pages linked (`Page.creators`) to the reference's own creator are not counted: a real
  leak spread across many sites looks exactly like common footage, and cross-site copies rarely
  cluster, so counting them would mark the strongest evidence as shared.
All orientations of a shared position are marked.

Shared frames are not evidence. Per reference and page, `ReferenceMatch` stores `matched_seconds`
(distinct reference seconds matched by non-shared frames), `shared_seconds` (seconds matched only
by shared frames), `evidence_frames` (distinct page FrameHashes matching a non-shared reference
frame) and `strength`: `strong` with at least 2 evidence frames, otherwise `weak`. Best distance
and timestamp come from non-shared frames when any exist. `matched_frames` still counts all
matching page frames. Weak and shared-only matches are stored so they remain reviewable; admin
filters by strength and shows the moments as mm:ss. Reports split counts by strength.

Extended by ADR 0015 (2026-09-15): page frames in active common-frame groups are discounted like
shared reference frames (`common_frames`, `common_seconds`); frames between the threshold and
`LOOSE_MAX_DISTANCE` (40) count as evidence only with a consistent timeline or cluster/mirror
corroboration (`loose_frames`); every stored match carries a timeline score, confidence and band.
The strict threshold stays 31.

Known limits: two page frames can be the same picture (e.g. poster and a sprite tile), and a leak
seen through a single preview frame stays weak. The page-group rule relies on creator links: a
leak copied to many pages that are not linked to the creator (missing or wrong name match) and
not clustered can still be marked shared.

Upsert evidence without changing human status, and remove only stale proposed matches within
the selected references. Confirmed/rejected rows survive even if their evidence disappears.
Reruns on overlapping references serialize with row locks. Link matching pages to their active
(non-rejected) cluster membership at match time; this marks a cluster as touched without creating
matches for unobserved members or changing cluster memberships. Match summaries reflect current
fingerprint hits, including hits a human rejected, and list creator-linked pages without a hit.
Those name-linked pages are a recall helper, not verified ground truth.

`reference_report` prints confirmed, rejected (false matches), and proposed (unreviewed) counts,
per reference and overall. Cluster coverage uses current active memberships, excluding rejected
reference matches from matched pages. Totals deduplicate pages across references. Human decisions
retained without current evidence still count as decisions. Admin supports review; import is CLI
only, with no upload or stored-file field.

## Consequences

Dihedral hashes cost up to eight times the index memory but handle rotations and mirrors.
Decoded media memory stays bounded to a frame; reference fingerprints and match aggregates live
in memory. Long imports hold a transaction to ensure failures leave no partial reference.
Threshold 31 remains a starting point; creator originals and human review must establish actual
recall and false-match rates before claims about detection accuracy.

## Ground truth and M2 report

`ReferenceLeak` records independent human ground truth per unique (reference, page), with
proposed/confirmed/rejected status, suggested/manual/match provenance, ranking score and signals,
review note and timestamps. Confirmed `ReferenceMatch` rows also establish ground truth without
requiring duplicate leak rows. Suggestions never decide identity for the operator.

`matching.leak_scoring` is pure. Candidate pages either link to the creator or share title tokens
with the original. Tokens are lowercase ASCII alphanumerics, with creator names/aliases, common
stopwords and generic site words removed. Title similarity is token Jaccard. Ranking uses the maximum applicable weight:
strong ReferenceMatch 1.00, weak ReferenceMatch 0.90, cluster/mirror evidence 0.80,
creator-linked duration within 1.5 seconds (inclusive) 0.50. Minimum score is 0.50.
Title similarity remains a displayed signal and contributes no score. Creator links alone
contribute no score. Duration-only candidates require the creator link and the named
`EXACT_DURATION_MAX_DIFF_S` threshold. Matches of any strength and cluster/mirror candidates
remain eligible with any duration, including missing duration and short clips.


`suggest_leaks [--reference ID] [--creator NAME] [--limit-per-reference 5] [--dry-run]` ranks
eligible pages deterministically by score then page ID. Human decisions in either table exclude
suggestions. Evidence-backed candidates (a ReferenceMatch of any strength, or a cluster/mirror
relation) are never capped; only duration-only candidates are limited to the top N per reference
(changed 2026-09-15, ADR 0015: the cap used to drop evidence-backed pages beyond five). Proposed
rows refresh evidence; proposals no longer selected (including below-threshold or
no-longer-candidate pages) are deleted. Human leak statuses and notes survive.
Writes serialize on the reference lock, as matching does; dry runs perform no writes or locks.
Admin provides text-only page links and evidence, filters and confirm/reject actions; no media
is fetched or embedded.

`m2_report [--creator NAME] [--max-distance 31]` is read-only. Its unit is a reference/page pair,
including totals: the same page can be a leak of several originals. Ground truth is the union of
confirmed leaks and confirmed matches. Found counts split stored matches within the requested
inclusive distance by strong/weak; recall uses all confirmed pairs as denominator. A reviewed
match has a confirmed or rejected decision in either table. Rejection in either table counts as
a false match, even if decisions conflict; each reviewed match counts once. Review rates include
all stored matches, including retained human decisions, independently of the distance cutoff.
Proposed suggestions and matches are listed separately as review workload.

Unmatched confirmed pairs report removal, absent hashes with media statuses (including expired
skips), sparse frames, low quality, or nearest Hamming distance against quality-eligible non-shared
reference frames. Missing reference evidence is explicit; an in-threshold distance prompts a
matching rerun/evidence inspection. The report does not recompute or modify stored matches/shared
flags. Exit passes at ≥70% strong+weak+linked recall and ≤5% false matches with nonempty denominators;
fewer than ten confirmed leaks always carries “too few to judge”, even if numerical targets pass.

A confirmed pair without its own in-threshold match counts as found **linked** when its page is an
active (non-rejected) member of a cluster that also has an active member with a current strong
match within the distance for the same reference, and that page is not rejected in either table.
Detection reached such a page through the cluster (e.g. a mirror whose poster is still pending),
not through its own frames. The report shows found strong/weak/linked and recall for strong,
strong+weak and strong+weak+linked; the exit check uses and names strong+weak+linked. Linked pages
get no miss reason. First review, 2026-09-15 (read-only): 3/3 found, page 2616 linked via cluster
306 to strong page 48.


### First M2 review: queue and propagation

After expiring/tokenized URLs, media claims prioritize pages linked to creators with any
Reference, using an indexed through-table Exists query. Newest media wins within each priority.

After matching, current strong non-rejected hits propagate proposals to other active members
of their current cluster, and recognized poster mirrors. Rejected leak decisions cannot seed
propagation. These ReferenceLeak rows have source `cluster`, score 0.80 and signals naming the
matched page and relation kind. All supported members are proposed, independently of the
suggestion command's limit. Human statuses and notes in both tables remain authoritative.
Only stale proposed cluster-source rows are removed when the current basis disappears,
including retained human matches that no longer have current strong fingerprint evidence.

Read-only evaluation on 2026-09-15, ranking before excluding human labels (top five per reference):
reference IDs 1–8 produced 2, 0, 0, 0, 1, 1, 0, 1 suggestions respectively.
Previously rejected counts were 0, 0, 0, 0, 1, 0, 0, 1. All three confirmed pages
(48, 2616, 1937) survived: 2 rejected versus 117 previously (98.3% fewer).
The transaction explicitly enforced READ ONLY. This is a retrospective check on eight originals,
not an independent accuracy estimate.


## Local fingerprint files (2026-09-15)

Originals may also be fingerprinted on the operator's machine (issue #27): `client/fingerprint.py`
(a uv script without Django) runs the same `matching.fingerprint.fingerprint_video` as
`add_reference` and writes a JSON file (format `marceline-fingerprint` v1) with sha256, size,
duration, fps, title, optional creator, software versions and the PDQ hashes; no video data, paths,
host or user names. `import_fingerprints` validates files strictly and stores them through
`import_fingerprint`, the same atomic import `add_reference` uses; it never trusts a file it cannot
fully validate, and it only claims the digest the client computed. `Reference.pipeline` records the
hashing recipe (`ffmpeg-rgb512-pdq-v0`) so a future canonical or browser pipeline is never compared
bit-for-bit with it, and `Reference.client` records where hashes were computed (`source`:
`server` or `local-cli`). A different ffmpeg build or CPU can move hashes by a few bits;
`import_fingerprints --compare` measures this against an original already imported on the server.
The local client needs its own watchdog (12 h instead of one hour).

## Several creators per original (2026-09-16)

Originals routinely show several creators (studio productions, collaborations), but `Reference` had a
single `creator` foreign key. Operators imported with one primary creator and typed the rest into
`source_note` free text, where nothing downstream could use them (issue #64).

`Reference.creators` (many-to-many to `catalog.Creator`, related name `referenced_in`) now holds
everyone in the scene. `Reference.creator` stays and keeps its meaning: the **primary creator**,
the rights holder whose takedowns we file, and the link `matching` uses wherever exactly one
creator is meant. The invariant is that the primary creator is always also in `creators`. It is
enforced in the service layer, not by a database trigger: `matching.creators.set_creators` writes
both sides inside the importing transaction, and every import path calls it. A trigger would have
to fire on a join-table write Django performs after `save()`, and would turn a bad import into a
database error inside a worker instead of a refused import. The read helpers (`creators_of`,
`creator_ids`, `creator_names`, `creator_label`) always put `reference.creator` first, so a row
whose join table was never written still reports its primary creator.

Wherever a creator is used for filtering or linking the choice is **all creators**, because a page
found under a co-performer's name is evidence about this very original:
- `mark_shared_frames` / `find_shared_frames`: the rule that pages linked to the reference's own
  creator do not count towards `COMMON_PAGE_GROUPS` now considers every linked creator
  (`SharedCandidate.creator_ids`). Otherwise a leak spread across pages tagged only with the
  co-performer would mark the strongest evidence as shared footage.
- `matching.leaks.rank_candidates` and the review queue's live scoring: a duration-only candidate
  needs a creator link, and a link to any performer satisfies it; every performer's names and
  aliases are stripped from title tokens. Both sides use the same rule, so the score shown in the
  queue still matches the stored one.
- `matching.capture_triage`: the `exact_duration_single_thumb` source follows that same link rule.
- `matching.media.claim_media`: media of pages linked to any performer of any original are
  prioritised.
- `references.select_references(creator=…)`: finds an original by any of its creators.
- `match_references`' "creator-linked pages NOT matched" recall helper: any performer's pages.

Primary-only, deliberately: the `Reference.creator` foreign key itself (rights holder,
`on_delete=PROTECT`) and the review API's `creator_name`. Reports, admin labels and the UI show the
full cast next to it as `creator_names` / "Sasha Meyer + Carla Bremer" (`creator_label` on the
server, `creatorLabel` in the browser).

Fingerprint files gain **format version 2**: `creator` is a list of names, the primary creator
first. Version 1 (one name or null) is still read; `to_json` always writes version 2. Names are
validated as strictly as before: non-empty strings of at most 255 characters, no case-insensitive
duplicates, at most `MAX_CREATORS` (20). `add_reference --creator` and `import_fingerprints
--creator` are repeatable, the first occurrence primary; an unknown or ambiguous name still fails
that file with `resolve_creator`'s message rather than importing a partial cast.

## Photo collections (2026-09-16)

An original may also be a photo collection: `Reference.kind = photos`, one reference per whole
set, N images with no timeline, its own pipeline tag and its own evidence rule. See **ADR 0020**;
everything above about atomic imports, creator resolution, shared frames and human review is
unchanged for them.

The data migration backfills `creators` from `creator` for existing rows. It deliberately does not
parse `source_note`: the note has no format, and guessing names would link the wrong creators.
Originals whose co-performers are recorded there (reference 13, the Nordlicht series 9–12, and the
"Sasha Meyer / Carla Bremer" original) must be corrected by hand after merging.
