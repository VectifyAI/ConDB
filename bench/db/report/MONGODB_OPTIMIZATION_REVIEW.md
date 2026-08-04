# MongoDB optimization review

Reviewed: 2026-08-05

Canonical report: `bench/db/report/report.tex` / `report.pdf`

## Decision summary

The main report's MongoDB measurements are internally consistent and the three
reported interventions have output-equivalence checks. Their adoption status is
not the same:

| Intervention | What is verified | Current decision |
|---|---|---|
| Coalesce already-known node, parent, or entity IDs | Two grouping seeds, exact output checks, listener-free wall time, and a control against individual requests submitted through at most 16 workers | Promising in the MongoDB storage harness. Not implemented in a MongoDB ConDB backend; group formation, remote RTT, and API-level load remain unmeasured. |
| Change `Cursor.batch_size` for subtree reads | 100 matched paths, five repeats, exact ordered-output checks, separate listener-free latency and command-count telemetry, ID-only and covered-Metadata projections | Reject manual tuning for the covered-Metadata path. The large request has no meaningful covered-Metadata gain; small batches regress. Keep the driver default. |
| Store consecutive DFS rows in bounded bucket documents | Frozen five-size selection, 100-root holdout, seven paired repeats, a 67-root range-disjoint sensitivity, 250 unused small/deep roots, full 10M-row digest, BSON-size check, and an independent live output audit | Promising only for the measured large-subtree cohort. Do not adopt until the size estimator/router, mutable-tree updates, atomic publication, multi-tree behavior, and end-to-end API path are measured. |
| Activate a private direct, non-deduplicating `CountStage` -> `CountScan` protocol on MongoDB master | Public-output, CommonStats, injected-yield, multikey-fallback, classic-core, and aggregation correctness checks; ten logging-suppressed process pairs with five raw repetitions per arm | Local source candidate only. The A/B isolates activation within one patched master snapshot; it is not an upstream, 7.0.34, or end-to-end ConDB comparison. |
| SQLite Beam batching | Real `TreeDB` and `BeamRetriever` code, file-backed latency control, exact result fingerprints, and 1/4/16-client load control | Implemented and tested, but it is a cross-engine implementation check. It is not evidence that MongoDB production latency improved. |

No MongoDB optimization described by the main report is currently integrated
into ConDB's public storage path. The executor candidate is committed only on
the `carsontung666/mongo` fork and has not been merged into upstream MongoDB or
a MongoDB release.

## MongoDB source candidate audit

The report's service and ConDB storage-harness baseline remains tag `r7.0.34`
for reproducibility. Source development targets current master snapshot
`5d3b36cf3871846fe7894616e964cb520c11d473`, because a patch intended for
review should not be built on a maintenance tag. This is a source target
decision, not evidence that master is faster than 7.0.34.

The source audit rejected redundant or unsafe directions before implementation:

| Candidate direction | Audit decision |
|---|---|
| Reintroduce an `_id` fast path | Reject as redundant: current master already routes eligible point queries through EXPRESS planning. |
| Add classic batched `getNext` handoff or repeat known SBE IXSCAN refactors | Reject as redundant: the relevant batching/refactoring is already present on master. |
| Generalize compound unique equality conjunctions into EXPRESS | Reject for this round. A correct gate must prove full canonical-key coverage and account for multikey, collation, sparse/partial indexes, sharding, projection, and planner semantics. A multikey counterexample invalidates a broad rewrite. |
| Avoid unused `WorkingSetMember` materialization under direct count | Accept narrowly. The optimization is gated to a direct, non-deduplicating classic `CountScan`; deduplicating and standalone consumers retain the public path. |

Two earlier prototypes were discarded rather than benchmarked as final
evidence. Returning `ADVANCED` with an invalid WorkingSet ID violated the public
PlanStage contract; reusing one live member bent generic ownership/lifetime
expectations. The committed design instead uses a private friend protocol while
leaving public `CountScan::work()` materialization intact. No measurements from
the rejected prototypes appear in the report.

## Superseded component claims

The local `optimization.tex` is a superseded working draft and must not be
shipped as a second final report.

Its headline 15.5 ms P50 / 168 ms P95 result is a covered descendant-ID scan,
not the report's full ordered descendant-Metadata operation. In the same table,
resolving Metadata for every returned node costs 96.3 ms P50 / 1,034 ms P95,
versus 38.3 ms / 421 ms for the two-collection no-Text view anchor, before a
complete endpoint's tree reconstruction and formatting. The three-collection
Structure/Metadata/Text layout is therefore a candidate component design, not a
validated final schema.

The draft also calls structural denormalization untested even though the newer
main report measures DFS buckets. Its Nested Sets discussion must not claim that
changing path keys alone removes per-document or per-index-entry work: the
existing Materialized Path implementation is already one contiguous index range.
The full-decoupling experiment also omits the tenant-qualified point and parent
indexes and does not regression-test the other three operations.

The draft's repeated "MongoDB product team confirmed" and "version bump would
not change" statements have no reviewable reply, date, ticket, or 8.x benchmark
in this repository. They must not support a published conclusion without a
citable record. At most, the 7.0 experiment establishes the measured access path;
absolute latency on newer server versions remains unmeasured. The patched
master source microbenchmark uses a different workload and an activation-
disabled candidate control, so it is not a 7.0.34-to-master version comparison.

The draft describes an average 36,456.7-row subtree as "up to" or "worst case."
For its 200-root cohort the stored row counts range from 5,006 to 1,404,566
(middle pair 11,656/11,686; P95 126,050), so 1,000-ID Metadata chunks vary from 6 to 1,405
calls rather than a fixed 36. Its P95 cannot be explained as a 36-call endpoint.

## Evidence map

The following locally frozen artifacts were re-read during this review. Their
bytes match the SHA-256 values below.

### MongoDB master direct-count source experiment

The frozen bundle is
`bench/db/report/evidence/mongodb_master_countscan_20260805/`. Its manifest
`SHA256SUMS` has SHA-256
`a9649df5c9caa3aac1b93b2f5f3d2571218067217a4bdf233c2332a0e8a8bb34`.

| Item | Identity or result |
|---|---|
| Upstream snapshot | `5d3b36cf3871846fe7894616e964cb520c11d473` |
| Candidate commit | `675598a071ca208c875ac7cf1874234fa644dd24` |
| Enabled binary SHA-256 | `5124a7cfab2ad6c5480a441f7665339df44d327eb018197f675b4399c15388f5` |
| Activation-disabled binary SHA-256 | `e55c61666c362b1baf506e3b157b04a9cdd277cd08dceb8f00ef2dbf3d3da468` |
| Retired-instruction reduction | 4.812071%, paired-bootstrap 95% interval [4.810965%, 4.813114%] |
| Benchmark-thread CPU-time reduction | 4.876567%, interval [3.252197%, 6.536442%] |
| Wall-time reduction | 4.888550%, interval [3.254416%, 6.559018%] |

The control is not unmodified upstream. Both arms are built from the candidate
commit and contain all candidate implementation and benchmark-harness code;
`baseline_disable.patch` additionally removes only the constructor activation block. The 20
raw JSON files contain ten process pairs, five raw one-iteration repetitions
per arm. `analyze.py` validates those invariants and reproduces `summary.json`
with a 100,000-sample complete-pair bootstrap (seed 20260805). The final fixture
suppresses timed slow-query logging. Targeted validation passed the two count
dbtest suites, four forced-classic core JS files and two aggregation JS files.
The frozen resmoke reports contain 22 and 12 passing result entries respectively,
including fixture and hook events. These are not a full MongoDB qualification
or production workload.

| Superseded-draft evidence | SHA-256 |
|---|---|
| `bench/db/runs/all_ops_layouts_20260723/all_ops_10m.json` | `249227a8da6360becca5692b1776004a34af2e58eb5c1d0ac27f9a9502a168d1` |
| `bench/db/runs/subset_kv_large.json` | `7800dc962c4cf47aa4551cd4dbab22a9b37b5592729c608bf52f9a365ae07fee` |

### Short-read coalescing

| Artifact | SHA-256 |
|---|---|
| `bench/db/runs/short_ops_batching_20260728/wall_pymongo417_v2_seed_20260728.json` | `e55baf4e0bd395c33576dc535506cc4e4bf8929530aa018399eda94260236c04` |
| `bench/db/runs/short_ops_batching_20260728/wall_pymongo417_v2_seed_20260729.json` | `d1fc1f33e815621f1a9b34d32ba970bf9bc2f80b4937133ec0d598a3cd58012e` |
| `bench/db/runs/short_ops_batching_20260728/threaded_control_16w_pymongo417_v3_seed_20260728.json` | `cee130e3a3f5332408acdc6848709355b9d262fb53acc9446d5eb2b117457074` |
| `bench/db/runs/short_ops_batching_20260728/threaded_control_16w_pymongo417_v3_seed_20260729.json` | `5d08e858aeb7d537e43975434735a3d4093d38881732427af942022c3b13b2a1` |

Each wall-time seed contains 9,600 baseline/candidate output checks. Each
threaded control contains 1,440 three-arm output checks. The reported B=3 and
B=64 P50 ratios match the stored summaries.

### Cursor batching

| Artifact | SHA-256 |
|---|---|
| `bench/db/runs/rootcause_20260728/mongo_batch_idonly_wall_v3_100x5.json` | `36b5e4c32268cc3050d1a84877c7697ad01ed04bf2eb5f2d9830edac6dda602b` |
| `bench/db/runs/rootcause_20260728/mongo_batch_covered_wall_v3_100x5.json` | `0aad9b6db7fbb03e77ddab7823e8e83032018f6f6f5e88fceb94ccb0a9cfe51e` |

Each artifact has 2,000 exact ordered-output checks. The ID-only paired median
speedup at a one-million requested batch is 1.0495 with a descriptive 95%
root-bootstrap interval of [1.0383, 1.0560]. The covered-Metadata value is
1.0047 with [0.9975, 1.0096], which does not support adopting a manual batch
size for the actual Metadata projection.

### DFS buckets

| Artifact | SHA-256 |
|---|---|
| `bench/db/runs/subtree_buckets_20260727/selection_holdout_v3_final.json` | `39532ead8837225cf86c8824bc442f3818a10396582524b571c93127b32bcdfe` |
| `bench/db/runs/subtree_buckets_20260727/selection_holdout_v3.selection_freeze.json` | `9b84a87755492e4e7806dde0a8939b964de472ba0f5aad0eb0c783217ed58080` |
| `bench/db/runs/subtree_buckets_20260727/selection_holdout_v3.output_audit.json` | `7f6630c0cc573b79e527476759df4427b43906a19d05b9cc78b83c66e1bc9709` |
| `bench/db/runs/subtree_buckets_20260728/selection_holdout_v3_independence_audit.json` | `5cfdd179a9b07313e331928f91d5ebca129539210e8e6d032d6959a33d59bcc5` |
| `bench/db/runs/subtree_buckets_20260728/build_v3_256_provenance.json` | `0c06b7e5e41bbf46bb408df75edf82721cfff0f3653437e29e72aec22028483c` |
| `bench/db/runs/subtree_buckets_20260727/spectrum_unbiased_v3.json` | `78688968dc06a75a2d86f00eefb59fda9b9a61dbacd3837c7b81762aa0c69c5f` |

The holdout summary reproduces the report's 1.5691x / 1.7229x marginal P50/P95
ratios. The strict 67-root sensitivity reproduces 1.5643x / 1.6670x. The build
contains 39,063 data buckets plus one manifest document; all 10M source rows are
present exactly once, the digest matches, and the maximum bucket is 124,221
bytes. The independent audit re-read selection, holdout, and spectrum outputs
with zero ordered-output mismatches. It did not collect replacement latency.

## Remaining adoption gates

Before presenting a MongoDB optimization as production-ready, measure:

1. the real ConDB endpoint, including root inclusion, depth limit, stable order,
   Metadata merge, tree reconstruction, formatting/serialization, and selected
   Text fetch;
2. real PageIndex trees and request traces, multiple trees, realistic IDs and
   path lengths, and repeated campaigns with paired uncertainty;
3. cold and cache-constrained runs, remote RTT, replica-set behavior, and mixed
   read/write load;
4. bucket-size estimation, routing threshold/overhead, false-route cost, update
   and split policy, rebuild cost, and atomic publication/failure recovery;
   the current builder can recursively split an oversized bucket while its
   validator assumes every non-final bucket has the configured row count, a
   latent invariant conflict not triggered by the 124,221-byte synthetic maximum;
5. enforced tenant scoping. One safe design is a leading `tree_id` predicate
   and index key; alternatives include namespaced globally unique IDs or
   collection/database isolation. The single-tree experimental ranges do not
   establish any multi-tenant design;
6. the real cost of the wide covering index that carries `title` and `summary`:
   footprint and cache residency with production field/path distributions,
   index build time, write amplification, and total retained storage after
   removing experiment-only control indexes. The reported bucket `+20.4%`
   denominator retains five source indexes and is not a minimal production-cost
   estimate;
7. a durable archive for the raw JSON evidence. `bench/db/runs` is gitignored,
   so a repository clone currently contains this manifest but not the frozen
   storage-harness measurements needed to recompute it. The smaller master
   CountScan experiment is the exception: its complete evidence bundle is now
   versioned under `bench/db/report/evidence`;
8. before proposing the source candidate upstream, run the applicable full
   Evergreen matrix and longer server-process workloads, including concurrent
   yields and multiple selectivities. The private wrapper deliberately mirrors
   `PlanStage::work()` statistics and timing; future base-class changes must keep
   those two paths synchronized.

P95 in the main tables is usually a percentile across per-input medians. It
describes workload heterogeneity and must not be presented as an open-loop
service tail for repeated instances of one request.
