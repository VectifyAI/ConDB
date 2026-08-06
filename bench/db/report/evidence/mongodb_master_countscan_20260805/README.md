# MongoDB master CountScan source experiment

This directory freezes the final, logging-suppressed A/B used by the canonical
report. It is separate from the report's MongoDB 7.0.34 ConDB storage-harness
measurements.

## Source identity and tested change

- Upstream master snapshot: `5d3b36cf3871846fe7894616e964cb520c11d473`
- Candidate commit: `675598a071ca208c875ac7cf1874234fa644dd24`
- Fork branch: `carsontung666/mongo:agent/condb-query-hotpath`
- Draft PR: <https://github.com/carsontung666/mongo/pull/1>
- Candidate patch: `mongo_candidate.patch`

The candidate gives a direct, non-deduplicating classic `CountStage` ->
`CountScan` pair a private resultless protocol. The parent needs only execution
state, so that private path avoids creating and freeing one unused
`WorkingSetMember` per matching key. Public `CountScan::work()` continues to
return a fresh valid WorkingSet ID. Multikey and compound-wildcard scans keep
the existing materializing and deduplicating path.

This is an activation ablation, not candidate-versus-upstream or
7.0.34-versus-master. Both arms are built from the candidate commit and contain
all candidate implementation and benchmark-harness code. The control
additionally applies `baseline_disable.patch`, which removes only the constructor
block that activates the private protocol.

| Arm | SHA-256 | ELF build ID |
|---|---|---|
| activation-disabled control | `e55c61666c362b1baf506e3b157b04a9cdd277cd08dceb8f00ef2dbf3d3da468` | `159f368f42c20b67521fd0215dbb32142a8b21c3` |
| enabled candidate | `5124a7cfab2ad6c5480a441f7665339df44d327eb018197f675b4399c15388f5` | `8997a7827fff4aba2700655dc26dfac0f6af7e03` |

The final rebuilt `bazel-bin/src/mongo/db/query/count_query_bm` matched the
enabled binary byte for byte.

## Build and benchmark protocol

Both arms used:

```text
BAZELISK_HOME=/tmp/mongo-bazelisk /home/junyao/.local/bin/bazel build \
  --config=opt --config=local --//bazel/config:build_enterprise=false \
  //src/mongo/db/query:count_query_bm
```

The benchmark creates 500,000 scalar documents and a `scanKey` index, then
runs a hinted scalar count over all keys. An untimed warmup checks command
status and requires `n == 500000`. The final benchmark fixture calls the
`BenchmarkWithProfiler` base setup, so timed slow-query logging is suppressed;
all 20 retained process logs contain zero slow-query or error/fatal records.
The classic-core explain regressions separately exercise `COUNT_SCAN`; the
benchmark command itself is fixed in the committed candidate source.

`run_blocks.sh` executes ten matched fresh-process pairs with five raw
repetitions per arm. Its predeclared order has five control-first and five
candidate-first pairs. Every retained raw row has `iterations=1`. The host was
a dual-socket Intel Xeon Gold 6418H system (48 physical cores, 96 hardware
threads) running Linux 6.8.0-84-generic. Processes were not CPU- or NUMA-pinned.

For each metric, `analyze.py` first takes the arithmetic mean of the five raw
rows inside each process. It forms ten candidate/control ratios, reports their
geometric mean, and resamples complete process pairs 100,000 times with seed
`20260805`. The interval is the percentile 95% paired-bootstrap interval.

## Results

Values in the first two columns are geometric means of the ten process-block
means. Time is per timed count.

| Metric | Activation-disabled | Enabled | Reduction | 95% CI for reduction |
|---|---:|---:|---:|---:|
| Main-thread user-space retired instructions | 1,288,739,491 | 1,226,724,429 | 4.812071% | [4.810965%, 4.813114%] |
| Benchmark-thread CPU time | 125.860 ms | 119.722 ms | 4.876567% | [3.252197%, 6.536442%] |
| Wall time | 125.895 ms | 119.740 ms | 4.888550% | [3.254416%, 6.559018%] |

All ten pairs favor the enabled arm for all three metrics. A separate
order-stratified sensitivity keeps the direction in both order groups. Wall
time is auxiliary because it closely tracks CPU time. The benchmark's
`cycles_per_iteration` is not treated as an independent PMU result.

## Correctness and integration validation

- Optimized Bazel build passed for `mongod`, `mongo`, `dbtest`, and
  `count_query_bm`.
- `dbtest --suite=query_stage_count_scan --suite=query_stage_count` passed,
  including public-output, CommonStats, injected `NEED_YIELD`, and multikey
  fallback checks.
- Four forced-classic core JS files passed; `validation/core.json` freezes 22
  passing result entries including fixture and hook events.
- Two forced-classic aggregation JS files passed;
  `validation/aggregation.json` freezes 12 passing result entries including
  fixture and hook events.
- Repository formatter, `git diff --check`, YAML parsing, and query benchmark
  suite selection passed.
- Three independent final reviews found no P0/P1/P2 issue after the benchmark
  integration fixes.

These targeted results are not a full MongoDB qualification run.

## Claim boundary

The evidence covers one warmed, single-thread, in-process, full-range scalar
count on one host. CPU and instructions describe the benchmark thread, not the
whole process. It does not establish improvement for stock upstream master,
MongoDB 7.0.34, SBE, multikey or compound-wildcard deduplication, indirect plan
shapes, covered Metadata production, cold caches, concurrent or mixed load,
remote clients, the ConDB endpoint, or production latency.

## Reproduction and files

```text
python3 analyze.py > reproduced-summary.json
cmp summary.json reproduced-summary.json
```

- `raw/`: 20 Google Benchmark JSON files (ten matched process pairs).
- `logs/`: the 20 logging-suppressed process logs and initializer seeds.
- `summary.json`: checked aggregate output.
- `analyze.py`: validation, aggregation, and paired-bootstrap analysis.
- `run_blocks.sh`: frozen balanced execution order and benchmark command.
- `baseline_disable.patch`: the one-hunk activation ablation.
- `mongo_candidate.patch`: full committed MongoDB candidate patch.
- `validation/`: retained resmoke reports.
- `SHA256SUMS`: digest manifest for this evidence bundle.
