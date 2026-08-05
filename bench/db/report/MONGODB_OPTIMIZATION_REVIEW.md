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
| Let a direct `CountStage` -> `CountScan` pair skip an unread working-set member, on a pinned MongoDB master snapshot | Optimized and runtime-dassert count dbtest suites; 60 forced-classic resmoke result entries; a 542-field `explain` comparison against a separately built base binary showing no substantive difference, including on the bare-`CountScan` aggregation path (all three retained under `evidence/.../validation/`, run on `ac20554f`, which differs from the candidate by one include-order line); a pre-registered 450-process three-arm campaign | Local source candidate only, and its adoption gate did not pass. The instruction reduction against the pinned base is real and consistent (30/30 blocks, ~117 instructions per counted document), but both controls fell outside their bands, so no CPU-time or wall-time claim is made. Applies only to single-solution plans; every measured plan is hint-forced. Not an upstream, 7.0.34, or end-to-end ConDB comparison. |

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
`0561c098b99ac5e929005e70a2e37d7a97a82423`, because a patch intended for
review should not be built on a maintenance tag. This is a source target
decision, not evidence that master is faster than 7.0.34.

The source audit rejected redundant or unsafe directions before implementation:

| Candidate direction | Audit decision |
|---|---|
| Reintroduce an `_id` fast path | Reject as redundant: the pinned snapshot already routes eligible point queries through EXPRESS planning. |
| Generalize compound unique equality conjunctions into EXPRESS | Reject for this round. A correct gate must prove full canonical-key coverage and account for multikey, collation, sparse/partial indexes, sharding, projection, and planner semantics. A multikey counterexample invalidates a broad rewrite. |
| Avoid unused `WorkingSetMember` materialization under direct count | Accept narrowly. The opt-in is per instance and set only by a direct `CountStage` parent, so every other `CountScan` consumer keeps the public `RID_AND_OBJ` output contract. It applies only when `CountStage`'s child is a bare `CountScan`, i.e. the single-solution plan case; a multi-planned or plan-cached count is unaffected. |

The committed design went through three shapes. Reusing one live member across
advances bent generic ownership and lifetime expectations and was dropped. The
second shape added a `PlanStage::trackWork` template on the base class of every
classic stage so that a second entry point on `CountScan` could share timing,
`CommonStats` and failure accounting with the public `work()`; two independent
source reviews rejected it as disproportionate to one caller, and the campaign
below measures it as arm B, where it costs 24 retired instructions per fetched
document on a plan the optimization cannot even fire on.

The committed design is the third: `CountScan::doWork()` returns `ADVANCED` with
`WorkingSet::INVALID_ID`. That reads as a contract violation and was rejected on
those grounds in an early round, which was the wrong call — `CountStage::doWork`
already sets `*out = INVALID_ID` unconditionally as its first statement and never
reads its child's output, so the contract in question has exactly one consumer
and that consumer does not depend on it. What makes it safe is not the return
value but who can ask for it: the setter is private with `CountStage` as the only
`friend`, because `WorkingSet::get()` is checked only under `dassert` and a public
mutator would be an out-of-bounds read in release builds as soon as a second
caller appeared. The one-line comment in `plan_stage.h` exists so the base-class
contract does not become false. Deduplication, memory accounting and its limit,
`keysExamined`, and yield and save/restore handling all run above the skipped
step, so multikey scans still deduplicate and still report their peak tracked
memory. No measurements from the discarded prototypes appear in the report.

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
absolute latency on newer server versions remains unmeasured. The master
source experiment runs a different workload and compares three builds of the
same master snapshot against each other, so it is not a 7.0.34-to-master version
comparison either.

The draft describes an average 36,456.7-row subtree as "up to" or "worst case."
For its 200-root cohort the stored row counts range from 5,006 to 1,404,566
(middle pair 11,656/11,686; P95 126,050), so 1,000-ID Metadata chunks vary from 6 to 1,405
calls rather than a fixed 36. Its P95 cannot be explained as a 36-call endpoint.

## Evidence map

The following locally frozen artifacts were re-read during this review. Their
bytes match the SHA-256 values below.

### MongoDB master direct-count source experiment

The frozen bundle is
`bench/db/report/evidence/mongodb_master_countscan_20260805_4109dcc31ff6/`. The directory name
ends in the commit that was the candidate when it was created; that commit is now arm B. Read the
arm table, not the directory name.

| Item | Identity or result |
|---|---|
| Pinned base (arm A) | `0561c098b99ac5e929005e70a2e37d7a97a82423` |
| Rejected heavyweight implementation (arm B) | `4109dcc31ff6df595c6b2e5caf3fbce077c488ba` |
| Candidate (arm C) | `90814b83d3e55f099c1244266d86700b5f633972` |
| Fork review | `carsontung666/mongo`, [PR #1](https://github.com/carsontung666/mongo/pull/1), draft |
| Campaign | 30 blocks x 5 workloads x 3 arms = 450 fresh processes, 5 repetitions each |
| **Pre-registered adoption gate** | **did not pass** — both controls fell outside their bands |
| C/A scalar count, instructions | 4.600% fewer, adjusted 98.333% CI [4.598%, 4.601%], 30/30 blocks |
| C/A multikey count, instructions | 2.055% fewer, [2.051%, 2.060%], 30/30 blocks |
| C/A compound wildcard, instructions | 3.680% fewer, [3.678%, 3.683%], 30/30 blocks |
| Absolute saving | 118.0 / 116.1 / 117.1 retired instructions per counted document |
| CPU time and wall time | **no claim made** — see below |

The count-endpoint instruction results are the only thing claimed. They are the pre-registered
primary metric, the widest of their three adjusted intervals spans better than one part in 10^4, and
they favour the candidate in every one of the 30 blocks on all three endpoints. The saving scales
with counted documents rather than stage iterations, and the multikey workload is the only one that
can show this: it advances over 400,000 keys while counting 200,000 documents and still saves 116.1
instructions per counted document, which is what removing one working-set allocation, member
initialisation and free per counted document predicts. On the other two endpoints keys and documents
coincide, so they corroborate the magnitude but cannot distinguish the two normalisations.

**Why the gate did not pass, and what follows from it.** The point-query control is unchanged in
instructions (0.9997, 95% CI [0.99884, 1.00055]) but its CPU-time interval was not wholly inside its
band: against [0.97, 1.03] the interval is [0.967321, 0.987021], so the 0.977 point estimate is
inside and the lower bound is not. That ~2.3% offset appears in all six arm-order strata, survives
minimum-based aggregation, and decomposes onto the arms (22920 / 22767 / 22396 ns for A / B / C)
rather than onto execution position (22721 / 22657 / 22706 ns for first / second / third in a
block). The change cannot execute on that plan, so it is a property of the binaries — most plausibly
code layout, though the campaign counts only instructions and elapsed time and cannot separate clock
from work.

The consequence is that **no CPU-time or wall-time result is claimed anywhere in this experiment**,
including for the count endpoints. This discards a result the protocol permitted rather than one it
denied: the pre-registered CPU non-regression gate *passed* for C/A on all three endpoints and set
`cpu_speedup_claim_eligible` true, with point estimates 0.956 / 0.962 / 0.935, which would have read
as a 4-6% CPU reduction. The same instrument reports 0.977 on the point query and 0.991 on the
`FETCH`-based count — two plans where the candidate's instruction count is unchanged — so several
percent of apparent CPU improvement is available to this binary on code the change never executes.

The un-optimized control (`COUNT -> FETCH -> IXSCAN`, where the optimization cannot fire) failed for
two further reasons. Its band was +/-0.2%, applied to a between-process ratio whose per-block
standard deviation turned out to be 1.57%, so it would have needed roughly 240 blocks. `campaign.json`
records a written justification for the 1% noninferiority margin but none for either control band, so
that band is best described as set by analogy with repetition-level reproducibility rather than from
any recorded analysis of between-process dispersion. The dispersion is a per-process two-state latch:
all 90 processes lie wholly in one of two states 2.32% apart (56 lower, 34 upper), none straddles them
at repetition level, and within a state *and* within an arm the coefficient of variation is 0.002%.
Pooling arms within a state leaves a 0.26% spread, which is the rejected arm's offset. Conditioned on
state, the candidate is indistinguishable from the base there (0.999990 over the 10 blocks where both
arms landed low, 1.000000 over the 7 where both landed high; sd 2.8e-5 and 2.2e-5).

That conditioning is **post-hoc**: it is not in the frozen `analyze.py`, and it conditions on a
post-treatment variable whose incidence differs by arm (upper state in 13/30 blocks for A, 7/30 for
B, 14/30 for C). `analyze_controls_posthoc.py` in the bundle states the threshold rule — cut at the
single widest relative gap in the sorted process means, 2.11% against a next-widest 0.25% — and
prints every conditioned figure beside its pre-registered marginal counterpart.

**What the control found instead.** The rejected implementation retires **24 more instructions per
fetched document (+0.256%)** than either the candidate or the base on that same non-firing plan
(state-conditioned B/A is 1.002558 over 14 concordant blocks in the lower state and 1.002499 over 4
in the upper). This is consistent with the cost of routing every classic stage's `work()` through a
new base-class helper — the only B-only change that executes on this plan, at roughly 8 instructions
across the three `work()` calls per document — but no arm carries that change in isolation, so the
attribution is an inference rather than a measurement. Note that the pre-registered marginal
estimator reports 0.9978 for the comparison and so inverts the sign: the rejected arm landed in the
high-instruction state 7 times in 30 against the base's 13. Both figures are reported.

On the three count endpoints the rejected implementation retires 0.33-0.45% fewer instructions than
the candidate (adjusted upper bounds 1.00453 / 1.00330 / 1.00329 against a pre-registered 1.01
noninferiority margin). Its extra saving is 11.0 / 9.0 / 10.0 instructions per **stage iteration**,
flat where the candidate's saving is per counted document, out of the 116.1-134.1 that either
implementation removes. That fits what B still does and C does not: replace the two virtual calls per
iteration between `CountStage` and `CountScan` with typed ones. Netted against its intrusion on the
non-firing plan, its additional machinery — a template on the base class of every classic stage, two
`friend` declarations and a second entry point — does not pay for itself.

**Provenance notes.** Three, all of which a reviewer should weigh.

1. The protocol was pushed 40 seconds before the first benchmark process, but it was not written
   blind. The attested build smokes had already run all five workloads at campaign size on all three
   arms, so the per-arm instruction levels — and with them the endpoint ratios — were visible when
   the protocol was frozen. Each of the three pre-registrations discloses this. What the 30-block
   campaign establishes is the intervals, the block-level consistency and the controls.
2. Attempt 003's pre-registration cites anchor commit `1c1d4287`, which holds the attempt-002
   protocol; the protocol that actually ran first appears in `1688ea3d`, committed 43 seconds before
   the run began. The pre-registration was written before its own commit existed and recorded the
   then-current HEAD. The freeze genuinely preceded execution; the citation was wrong. The correction
   is in `anchor_correction.json`, together with the record of a mistake made in filing it: it was
   first appended to the ledger as a ninth record with a `record_type` the frozen analyzer rejects,
   which made the whole bundle fail validation, so it was moved to that file and the ledger truncated
   back to the eight records the runner itself wrote. That line had never been committed or pushed.
3. Attempts 001 and 002 are retained: 001 was superseded before execution, 002 aborted on its first
   process because the analyzer asserted a google-benchmark field that iteration rows do not carry.
   Neither produced any measurement.

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
2. ~~add a pinned-base arm~~ — **done**. The superseded activation ablation shared
   its implementation and harness across both arms and so could not attribute the
   effect to the patch rather than to activation. It is replaced by a three-arm
   campaign against the pinned base, with a control workload whose plan shape
   prevents the optimization from firing at all;
3. validate planner and executor integrations beyond the forced-classic paths
   in the targeted matrix, while retaining the explicit exclusion of indirect
   and deduplicating plan shapes;
4. retain the public `RID_AND_OBJ` output assertion as a regression test. The
   earlier design routed both paths through a new `PlanStage::trackWork` helper;
   that helper was removed in review because it put an accounting side-door on the
   base class of every classic stage for one caller. The candidate instead reads a
   private flag inside the existing `doWork()`, so stage accounting is unchanged by
   construction and no shared helper needs protecting.

P95 in the main tables is usually a percentile across per-input medians. It
describes workload heterogeneity and must not be presented as an open-loop
service tail for repeated instances of one request.
