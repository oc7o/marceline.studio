# 0009 — Video clusters

Status: Accepted

Repeated uploads form groups, so pair review duplicates work. Store Cluster and ClusterMember,
with at most one active membership per page and durable rejected memberships. Clusters survive
member deletion and have no stored original. Display the earliest known uploaded_at, then
first_seen (unknown upload dates last, page ID breaks ties).

Pure clustering admits pages only with edges to a strict majority of active members. Seed new
clusters with two pages; never merge existing clusters automatically. Deterministic page-ID
order makes reruns stable, at the cost of order-dependent grouping where evidence is ambiguous.
Services collect PDQ, non-empty SHA256, and exact external URLs; exclude Telegram and URLs used
by more than five pages. No media is fetched.

Human SAME labels are transitive and can consolidate confirmed memberships without changing
their status. Preserve old clusters and rejection rows. Contradictory human decisions abort the
transaction for review. DIFFERENT labels prevent proposals and remove incompatible earlier
proposals. Each confirmed/rejected pair in a cluster also blocks automatic proposals in every
cluster. Two pages rejected from the same cluster cannot reuse either rejected membership's
evidence kind; a different kind can support a new proposal. Dry runs roll back the entire
workflow. Builders serialize with a transaction advisory lock. Cluster decisions supplement
evaluation; explicit decided PairLabels take precedence.

External-link evidence requires Jaccard similarity >= 2/3 of valid non-Telegram link sets
(including popular links), plus a PDQ candidate for that pair within max_distance (and the
configured score/quality thresholds). Shared hoster lists with similar durations are
insufficient: different scenes can have similar lengths. Duration neither establishes nor vetoes
evidence: clips are leaks and stay in their video's cluster. Human decisions remain
authoritative. Admin shows duration and flags possible clips shorter than the longest active
member by more than max(30 seconds, 3% of the longer duration); rejected members do not
establish that reference duration.

Reviewers can merge selected clusters into the lowest ID or move individual members in
the member admin. Services serialize these edits with builders and apply them atomically.
Duplicate pages retain CONFIRMED > PROPOSED > REJECTED; confirmed/rejected duplicates
abort. Explicit DIFFERENT labels and existing confirmed/rejected pairs cannot become
co-confirmed. Successful edits delete emptied source clusters and preserve winning row metadata.

An optional --review-max-distance adds a wider PDQ review band after normal assignments.
Normal proposals stay fixed; only additional members carry the borderline flag. The wider
band does not relax external-link corroboration. Rebuilds recompute proposals and discard
stale borderline rows when the option is omitted; confirmed/rejected rows remain untouched.
The admin exposes the flag and a filter for clusters with borderline proposals.


### CDN mirror posters (first M2 review)

HTTP(S) poster and frame URLs matching `MIRROR_POSTER_PATH_PATTERN` anywhere in the path
contribute each distinct `imgs/{a}/{b}/{15-hex}` mirror key once per page, accepting `_main.jpg`
and `_{digits}.jpg` suffixes regardless of host, path prefix or query string.
This captures site02/site04 CDN mirrors even while media is pending, without fetching.
The existing five-distinct-page evidence cap applies. Normal majority admissions and all human
decision protections remain in force; mirror evidence does not relax external-link corroboration.
