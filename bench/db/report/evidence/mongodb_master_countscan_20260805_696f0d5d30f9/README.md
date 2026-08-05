# MongoDB pinned-master CountScan activation ablation

This directory freezes the final source-level experiment used by the canonical
report. It is separate from the report's MongoDB 7.0.34 ConDB storage-harness
measurements and does not benchmark a ConDB operation.

## Source and review identity

- Pinned upstream snapshot: `5d3b36cf3871846fe7894616e964cb520c11d473`
- Candidate commit: `696f0d5d30f9bb6bcdb96ade8388e6bea36a92f9`
- Fork branch: `carsontung666/mongo:agent/condb-query-hotpath`
- Review: <https://github.com/carsontung666/mongo/pull/1>
- Full candidate diff: `candidate.patch`
- Activation-only ablation: `activation_disable.patch`
- Preregistered evidence-protocol commit in ConDB: `00fd8de`

The candidate activates a private resultless protocol only for a direct classic
`CountStage` -> `CountScan` pair for which `supportsResultlessCount()` confirms
`!_shouldDedup`. The private path avoids per-match `WorkingSetMember`
materialization and lifecycle. It shares `PlanStage::trackWork` with public
`work()` for timer, CommonStats, and failure accounting. Public
`CountScan::work()` still returns a valid `RID_AND_OBJ` WorkingSet member.
Multikey scans and scalar compound-wildcard scans continue to materialize and
deduplicate.

The backing-slot test observes zero slots throughout the direct path. This does
not mean that the whole query or executor performs zero allocations.

## Arm identity and build

This is an activation ablation, not candidate-versus-upstream. Both arms use
candidate commit `696f0d5d30f9` and contain the same implementation and
benchmark harness. The disabled control differs only by applying
`activation_disable.patch`, which removes the constructor activation block.

| Arm | SHA-256 | ELF build ID |
|---|---|---|
| B: activation-disabled control | `ed2bdc05a6188a0ebb6433923391417c183e3471df78516eac3523c2f825bebc` | `d14aea10c20ca862734eb72e930225a1e5ea263e` |
| C: enabled candidate | `02628346e4357ab9a48d5c0dea0de68df4c0b2921ded3fb44c4f643eb5c043be` | `482d4815330592895592815012509a756b70ccf8` |

The standard optimized build command was:

```text
bazel build --config=opt \
  //src/mongo/db:mongod \
  //src/mongo/s:mongos \
  //src/mongo/shell:mongo \
  //src/mongo/dbtests:dbtest \
  //src/mongo/db/query:count_query_bm
```

Each A/B arm rebuilt `//src/mongo/db/query:count_query_bm` with the same
`--config=opt` setting. After restoring the activation block, the enabled
binary rebuilt to the same SHA-256 shown above, making the retained enabled
identity reproducible within this checkout and build cache.

## Frozen protocol

ConDB commit `00fd8de` froze `campaign.json`, `run_blocks.sh`, `analyze.py`, the
two patches, the source identity, arm hashes, benchmark filter, sample count,
order, stopping rule, and analysis before execution. Its `campaign.json` has
SHA-256 `a1c495211544827370a4c64cea4d549b69250a8ff6092c66488a8a6213ce2404`.

- Benchmark: `CountQueryBenchmark/DirectNonDeduplicatingCountScan/400000/64`
- Input: 400,000 scalar documents with a scalar index and a hinted full-range
  count
- Preflight: exact count and strict `COUNT -> COUNT_SCAN` explain shape
- Host: Intel Xeon Gold 6418H, Linux 6.8.0-84-generic, performance governor
- Affinity: the whole benchmark process pinned to CPU 0 with `taskset -c 0`
- Sampling: 20 matched fresh-process pairs, 10 B-then-C and 10 C-then-B
- Technical repeats: five one-iteration rows per process; these do not make
  `n=100`
- Stopping: fixed-size campaign, no early stopping, no partial reruns
- Estimator: arithmetic mean of five rows within process, then geometric mean
  of complete-pair C/B ratios
- Interval: 100,000-sample order-stratified complete-pair bootstrap, fixed seed
  `20260805`, with the same resamples across metrics

The runner verified campaign, harness, binary SHA-256, and ELF build IDs before
and after all 40 processes. `campaign_run.json` records the execution order,
timestamps, host, governor, and the successful postflight identities.

## Results

The first two columns are geometric means of the 20 process-block means. Time
is per 400,000-key count.

| Metric | B: disabled | C: enabled | Reduction | 95% CI for reduction |
|---|---:|---:|---:|---:|
| Benchmark-thread user-space retired instructions | 1.035818430 billion | 0.979399111 billion | 5.447% | [5.445%, 5.448%] |
| Benchmark-thread CPU time | 95.507073 ms | 88.839978 ms | 6.981% | [5.425%, 8.616%] |
| Wall time | 95.690465 ms | 89.005484 ms | 6.986% | [5.428%, 8.622%] |

All 20 pairs favor activation on all three metrics. Both B-then-C and C-then-B
stratum point estimates favor activation, and every leave-one-pair-out estimate
remains below one. Retired instructions are the primary mechanism check;
benchmark-thread CPU time is secondary and wall time is auxiliary. Google
Benchmark's cycle estimate is not treated as independent PMU evidence.

## Correctness and integration validation

- The standard optimized five-target build passed for `mongod`, `mongos`,
  `mongo`, `dbtest`, and `count_query_bm`.
- `query_stage_count_scan` and `query_stage_count` passed in optimized and
  explicit runtime-dassert builds.
- Unit tests verify public backing-slot growth from zero to one; zero slots for
  the direct skip, limit, and injected-yield/save/restore path; and
  materializing/deduplicating fallback for multikey and scalar
  compound-wildcard scans.
- The benchmark preflight verifies the exact result and strict
  `COUNT -> COUNT_SCAN` plan before timing.
- Forced-classic resmoke passed six core JS files (`core.json`, 32 result
  entries), two aggregation files (`aggregation.json`, 12), one no-passthrough
  file (`no_passthrough.json`, 3), and one sharding file (`sharding.json`, 3).
  These counts include fixture and hook events.
- Formatting, buildifier, YAML parsing, lint, and diff-integrity checks passed;
  independent final code and evidence reviews reported no P0--P3 issue.

These are targeted checks, not MongoDB's full Evergreen qualification matrix.

## Claim boundary and remaining control

The evidence supports one warmed, single-thread, in-process, full-range scalar
count on one pinned host. It does not show an improvement for stock upstream,
MongoDB 7.0.34, SBE, indirect or deduplicating plan shapes, covered-Metadata
production, cold caches, concurrent or mixed load, remote clients, any timed
ConDB operation, or production latency.

Because both arms share the candidate implementation and benchmark harness,
the result isolates activation but does not estimate the full patch versus
upstream. A third pristine-base arm with a neutral harness remains an adoption
gate.

## Reproduction and file map

```text
python3 analyze.py > reproduced-summary.json
cmp summary.json reproduced-summary.json
```

- `campaign.json`: preregistered protocol and expected identities
- `campaign_run.json`: completed execution record and pre/postflight identities
- `raw/`: 40 Google Benchmark JSON process blocks
- `logs/`: 40 process logs
- `summary.json`: validated aggregates and sensitivity checks
- `analyze.py`: campaign validation, aggregation, bootstrap, and sensitivities
- `run_blocks.sh`: fixed order and fail-closed execution
- `candidate.patch`: full candidate diff from the pinned base
- `activation_disable.patch`: the single activation ablation
- `validation/`: optimized/runtime-dassert dbtests and frozen resmoke reports
- `REVIEW_SUMMARY.md`: independent implementation, history, method, and blank
  post-campaign review outcomes
- `SHA256SUMS`: digest manifest for every other retained bundle file
