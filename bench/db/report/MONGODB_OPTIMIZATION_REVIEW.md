# MongoDB optimization review

Reviewed: 2026-08-05

Canonical report: `bench/db/report/report.tex` / `report.pdf`

## Decision summary

The main report has four distinct MongoDB interventions. Their measurements
and output-equivalence checks are internally consistent, but their adoption
status is not the same:

| Intervention | What is verified | Current decision |
|---|---|---|
| Coalesce already-known node, parent, or entity IDs | Two grouping seeds, exact output checks, listener-free wall time, and a control against individual requests submitted through at most 16 workers | Promising in the MongoDB storage harness. Not implemented in a MongoDB ConDB backend; group formation, remote RTT, and API-level load remain unmeasured. |
| Change `Cursor.batch_size` for subtree reads | 100 matched paths, five repeats, exact ordered-output checks, separate listener-free latency and command-count telemetry, ID-only and covered-Metadata projections | Reject manual tuning for the covered-Metadata path. The large request has no meaningful covered-Metadata gain; small batches regress. Keep the driver default. |
| Store consecutive DFS rows in bounded bucket documents | Frozen five-size selection, 100-root holdout, seven paired repeats, a 67-root range-disjoint sensitivity, 250 unused small/deep roots, full 10M-row digest, BSON-size check, and an independent live output audit | Promising only for the measured large-subtree cohort. Do not adopt until the size estimator/router, mutable-tree updates, atomic publication, multi-tree behavior, and end-to-end API path are measured. |
| Activate a private direct, non-deduplicating `CountStage` -> `CountScan` protocol on a pinned MongoDB master snapshot | Standard five-target build; optimized and runtime-dassert count suites; public-output, backing-slot, skip/limit/yield, deduplication, strict-explain, classic-core, aggregation, no-passthrough, and sharding checks; 20 CPU-pinned fresh-process pairs | Local source candidate only. The A/B isolates activation within one patched snapshot; it is not an upstream, 7.0.34, or end-to-end ConDB comparison. |

No MongoDB optimization described by the main report is currently integrated
into ConDB's public storage path. The executor candidate is committed only on
the `carsontung666/mongo` fork and has not been merged into upstream MongoDB or
a MongoDB release.

SQLite Beam batching has a different boundary: it is implemented in the real
`TreeDB` and `BeamRetriever` path and has file-backed latency, result-
fingerprint, and 1/4/16-client controls. It is useful cross-engine evidence for
coalescing, but it is not a MongoDB intervention and does not show that MongoDB
production latency improved.

## MongoDB source candidate audit

The report's service and ConDB storage-harness baseline remains tag `r7.0.34`
for reproducibility. Source development targets the pinned master snapshot
`5d3b36cf3871846fe7894616e964cb520c11d473`, because a patch intended for
review should not be built on a maintenance tag. This is a source target
decision, not evidence that master is faster than 7.0.34.

The source audit rejected redundant or unsafe directions before implementation:

| Candidate direction | Audit decision |
|---|---|
| Reintroduce an `_id` fast path | Reject as redundant: the pinned snapshot already routes eligible point queries through EXPRESS planning. |
| Generalize compound unique equality conjunctions into EXPRESS | Reject for this round. A correct gate must prove full canonical-key coverage and account for multikey, collation, sparse/partial indexes, sharding, projection, and planner semantics. A multikey counterexample invalidates a broad rewrite. |
| Avoid unused `WorkingSetMember` materialization under direct count | Accept narrowly. The optimization is gated to a direct, non-deduplicating classic `CountScan`; deduplicating and standalone consumers retain the public path. |

Two earlier prototypes were discarded rather than benchmarked as final
evidence. Returning `ADVANCED` with an invalid WorkingSet ID violated the public
PlanStage contract; reusing one live member bent generic ownership/lifetime
expectations. The committed design instead uses a private friend protocol only
when the direct `CountScan` reports `!_shouldDedup`. Its resultless work and
public `work()` share `PlanStage::trackWork` for timing, CommonStats, and failure
accounting. Public `CountScan::work()` still returns a valid `RID_AND_OBJ`
member. Multikey scans and scalar compound-wildcard scans retain materialization
and deduplication. No measurements from the rejected prototypes appear in the
report.

### Prior-art and regression-history audit

| Prior change | Scope and relationship to this candidate |
|---|---|
| [MongoDB PR #635](https://github.com/mongodb/mongo/pull/635), landed through `de8cdc7779` and `3b3c25e571` | Fast-count bounds and `IndexBoundsBuilder::isSingleInterval` planning. It does not remove the executor's per-match WorkingSet lifecycle. |
| [MongoDB PR #1369](https://github.com/mongodb/mongo/pull/1369), landed as `14bfbd8833` | Elides `SHARDING_FILTER` when the full shard key is available. It does not change direct count-stage handoff. |
| `d71566a55e` (`SERVER-14098`) | Introduced `CountStage`, whose parent consumes execution state rather than a child result. This motivates the narrow optimization but did not establish the present public-output-safe protocol. |
| `dac2f722f8` (`SERVER-22133`) | Restored correct `COUNT_SCAN` generation from the plan cache and reinforces that generic/public consumers need a valid WorkingSet result. |
| `d8ee635331` (`SERVER-22407`) | Changed public `COUNT_SCAN` output from `OWNED_OBJ` to `RID_AND_OBJ`, recovering most of a reported regression while preserving a valid result contract. The candidate does not undo this. |
| `09b89f0986` (`SERVER-19377`) | Centralized stage timing/statistics around non-virtual `work()`. The candidate reuses the resulting accounting through `trackWork` rather than duplicating it. |
| `8f52dfc863` (`SERVER-75037`) | Made compound-wildcard `COUNT_SCAN` deduplicate independently of multikey status. The candidate explicitly leaves that path materializing and deduplicating. |

The inspected GitHub PRs are closed without a merged-PR marker, but their
changes landed through squash/commit-queue commits; they should not be called
abandoned. The GitHub audit and pinned-snapshot ancestry search found no existing copy
of the same private direct resultless optimization. That negative result is not
exhaustive of internal Jira context, private branches, or unpublished work.

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
`bench/db/report/evidence/mongodb_master_countscan_20260805_696f0d5d30f9/`.
The preregistered `campaign.json` was committed as ConDB commit `00fd8de`
before execution and has SHA-256
`a1c495211544827370a4c64cea4d549b69250a8ff6092c66488a8a6213ce2404`.
The checked `summary.json` has SHA-256
`420e7a6c148b2a2339984012cbbc28a344486f90a3328bcf2fb83f20248d4739`.

| Item | Identity or result |
|---|---|
| Upstream snapshot | `5d3b36cf3871846fe7894616e964cb520c11d473` |
| Candidate commit | `696f0d5d30f9bb6bcdb96ade8388e6bea36a92f9` |
| Fork review | `carsontung666/mongo:agent/condb-query-hotpath`, [PR #1](https://github.com/carsontung666/mongo/pull/1) |
| Enabled binary | SHA-256 `02628346e4357ab9a48d5c0dea0de68df4c0b2921ded3fb44c4f643eb5c043be`; build ID `482d4815330592895592815012509a756b70ccf8` |
| Activation-disabled binary | SHA-256 `ed2bdc05a6188a0ebb6433923391417c183e3471df78516eac3523c2f825bebc`; build ID `d14aea10c20ca862734eb72e930225a1e5ea263e` |
| Retired-instruction reduction | 5.447%, order-stratified paired-bootstrap 95% interval [5.445%, 5.448%] |
| Benchmark-thread CPU-time reduction | 6.981%, interval [5.425%, 8.616%] |
| Wall-time reduction | 6.986%, interval [5.428%, 8.622%] |

The control is not unmodified upstream. Both arms are built from the candidate
commit and contain all candidate implementation and benchmark-harness code;
`activation_disable.patch` additionally removes only the constructor activation
block. The 40 raw JSON files contain 20 fresh process pairs, with ten control-
first and ten candidate-first pairs pinned to CPU 0; five repetitions within a
process are technical repeats, not 100 independent samples. `analyze.py`
validates those invariants and reproduces `summary.json` with a 100,000-sample
order-stratified complete-pair bootstrap (seed 20260805). All 20 pairs favor the
enabled arm for all three metrics, both execution-order strata favor it, and
all leave-one-pair-out estimates remain below one. The runner verifies binary
and harness identities before and after the campaign.

The standard optimized build passed for `mongod`, `mongos`, `mongo`, `dbtest`,
and `count_query_bm`. Both count dbtest suites passed under optimized and
runtime-dassert builds. Tests establish public backing-slot growth from zero to
one, zero slots throughout the direct skip/limit/yield path, materializing
fallback for multikey and scalar compound-wildcard cases, and exact count plus
strict `COUNT -> COUNT_SCAN` explain shape. Forced-classic resmoke passed six
core JS files (32 result entries), two aggregation files (12), one
no-passthrough file (3), and one sharding file (3); result-entry counts include
fixture and hook events. These are targeted checks, not a full MongoDB
qualification or production workload.

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

### ConDB and storage interventions

1. the real ConDB endpoint, including root inclusion, depth limit, stable order,
   Metadata merge, tree reconstruction, formatting/serialization, and selected
   Text fetch, on real PageIndex trees and request traces;
2. for short-read coalescing, whether replacing a sequence of scalar reads with
   one `$in` query changes snapshot/visibility semantics under concurrent
   updates, and whether that change is acceptable for mutable trees;
3. group-level error, retry, timeout, cancellation, and partial-failure
   semantics. One failed or retried grouped command must not silently change the
   caller-visible outcome relative to the scalar contract;
4. duplicate and missing IDs, stable caller order, cross-tenant input rejection,
   and enforced tenant scoping. One safe design is a leading `tree_id` predicate
   and index key; alternatives include namespaced globally unique IDs or
   collection/database isolation;
5. `$in` cardinality, BSON command and response limits, response cursor
   behavior, and plan-cache effects for realistic and adversarial arrays;
6. multiple trees, realistic IDs and
   path lengths, and repeated campaigns with paired uncertainty;
7. cold and cache-constrained runs, remote RTT, replica-set behavior, and mixed
   read/write load;
8. bucket-size estimation, routing threshold/overhead, false-route cost, update
   and split policy, rebuild cost, and atomic publication/failure recovery;
   the current builder can recursively split an oversized bucket while its
   validator assumes every non-final bucket has the configured row count, a
   latent invariant conflict not triggered by the 124,221-byte synthetic maximum;
9. the real cost of the wide covering index that carries `title` and `summary`:
   footprint and cache residency with production field/path distributions,
   index build time, write amplification, and total retained storage after
   removing experiment-only control indexes. The reported bucket `+20.4%`
   denominator retains five source indexes and is not a minimal production-cost
   estimate;
10. a durable archive for the raw JSON evidence. `bench/db/runs` is gitignored,
   so a repository clone currently contains this manifest but not the frozen
   storage-harness measurements needed to recompute it. The smaller master
   CountScan experiment is the exception: its complete evidence bundle is now
   versioned under `bench/db/report/evidence`.

### MongoDB source candidate

Before proposing the executor candidate upstream:

1. run the applicable full Evergreen matrix and longer server-process workloads,
   including concurrent yields, multiple selectivities, empty/partial/full
   ranges, and repeated campaigns on additional hosts;
2. add a pristine pinned-base arm with a neutral benchmark harness. The current
   A/B is intentionally an activation ablation in which both arms share the
   implementation and harness, so it cannot attribute the measured effect to
   the entire patch versus upstream;
3. validate planner and executor integrations beyond the forced-classic paths
   in the targeted matrix, while retaining the explicit exclusion of indirect
   and deduplicating plan shapes;
4. retain public WorkingSet-output and backing-slot assertions as regression
   tests. Both paths now share `PlanStage::trackWork`; future accounting changes
   must continue to flow through that common helper.

P95 in the main tables is usually a percentile across per-input medians. It
describes workload heterogeneity and must not be presented as an open-loop
service tail for repeated instances of one request.
