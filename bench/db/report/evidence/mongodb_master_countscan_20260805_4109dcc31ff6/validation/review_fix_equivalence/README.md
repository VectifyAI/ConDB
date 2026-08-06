# The reviewed commit versus the measured commit

The 450-process campaign measured arm C = `90814b83d3e55f099c1244266d86700b5f633972`. Two later
blank reviews of the change, conducted as a MongoDB reviewer would read it, produced fixes. The
commit now under review is in `reviewed_commit.txt`. This directory shows what changed and that the
measured quantity did not move.

`measured_to_reviewed.diff` is the complete difference. Nothing in it executes inside
`CountScan::doWork()`'s per-key path.

- the `PlanStage::work()` contract comment, rewritten from a general permission into a closed
  exception naming `CountScan`, because as written it licensed any stage to return `ADVANCED`
  without a valid id;
- a guard in the setter for the case where the scan has already been worked.
  `ClassicPlannerInterface::makeExecutor()` and `buildRejectedExecutableTreesForExplain()` are two
  of the four `CountStage` construction sites and both wrap already-built rejected plan roots. The
  setter declines rather than asserting, so that if SERVER-118659 ever brings count scans under
  cost-based ranking the cost is this optimization rather than a thrown `explain`. A `dassert`
  catches the change in test builds. This runs once per `CountStage` construction, never per key;
- `setDoesNotMaterializeResults` renamed to `disableResultMaterialization`, and the flag moved next
  to `_shouldDedup` so it packs into existing padding rather than costing 8 bytes;
- the class docstring, which claimed unconditional `RID_AND_OBJ` materialization, and a comment on
  the member itself;
- test cleanups: a redundant `static_cast`, a `make_unique`/`release` round trip, one signed
  literal, and a docstring correction — `QueryStageCountScanDirectCountStageSemantics` is an
  invariance test that passes with and without the change, and now says so;
- `count_query_bm.cpp` and the `QueryBenchmarkFixture` helpers it needs, so a benchmark for this
  change ships in tree.

## What the shipped benchmark is, and is not

`count_query_bm.cpp` is **not** byte-identical to the harness the campaign ran. It is that file with
two things removed and one refactor applied:

- the `ClassicPointControlQueryBenchmark` fixture and its benchmark, which were evidence-only
  controls for the campaign and have no place in a count benchmark upstream;
- a `setUpSharedResources` override whose only effect was to call a base implementation that is a
  no-op, so it was dead code;
- the two copies of the `VersionInfoInterface` enable-then-explain sequence folded into one helper.

The three `COUNT_SCAN` benchmark bodies and the fetching-count control are unchanged. It compiles:
`build_reviewed.log.gz` is the `bazel build --config=opt //src/mongo/dbtests:dbtest
//src/mongo/db/query:count_query_bm` that produced the binaries used below.

## Measured check

The reviewed binary was smoked at campaign size on the same pinned CPU with the same command shape
the campaign used (`taskset -c 0`, `--benchmark_min_time=0.01`).

| Workload | Reviewed commit | Campaign arm C, mean of 30 | Ratio | Arm C range over its 30 processes |
|---|---|---|---|---|
| Scalar, 400k | 978,969,359 | 978,986,595 | 0.999982 | 0.0093% |
| Multikey, 200k | 1,106,166,214 | 1,106,058,624 | 1.000097 | 0.0168% |
| Wildcard, 200k | 612,750,402 | 612,754,586 | 0.999993 | 0.0150% |

**This is one process per endpoint against a 30-process mean, not a repeat of the campaign.** The
comparison column is arm C's *range* — the spread between its slowest and fastest of 30 processes —
not a standard deviation. Against arm C's per-process standard deviation instead, the scalar and
wildcard rows sit well inside it and the multikey row is about 2.3 standard deviations out, which in
absolute terms is +0.54 instructions per counted document against an effect of 116. So the effect
transfers; the bridge is a smoke check, and the intervals in the report remain pinned to the measured
commit.

A later re-smoke after the guard replaced the assertion gives 978,976,251 on the scalar endpoint,
a ratio of 0.999989 against the campaign mean (`bm_reviewed_S.json`).

The fetching-count control is deliberately omitted from that table. It carries the per-process
two-state instruction latch described in the report, so a single process cannot be compared against
a 30-process mean that averages both states. The smoke value, 1,875,900,909, sits in the lower
state, whose campaign mean is 1,875,801,579 — a ratio of 1.000053, inside the within-state spread.
`smoke_fixed_X.json` is retained so this can be checked.

## Behavioural checks on the reviewed commit

Both count dbtest suites pass: `query_stage_count_scan` 14/14
(`dbtest_reviewed_query_stage_count_scan.log.gz`) and `query_stage_count` 7/7
(`dbtest_reviewed_query_stage_count.log.gz`).

Against unpatched base `0561c098b99a`, with the four production files reverted and the tests kept,
the suite reports **874 TOTAL / 13 PASS / 1 FAIL**
(`dbtest_on_base_query_stage_count_scan.log.gz`). The failure is
`QueryStageCountScanMaterializationContract`, and the log records the assertion that fails:
`FAIL: Value of: WorkingSet::INVALID_ID == wsid`. `QueryStageCountScanDirectCountStageSemantics`
passes on base, as it must — every assertion in it is derived from `StageState`, which the change
does not touch.

The resmoke and explain-parity evidence in the parent directory was gathered on `ac20554f`, which
differs from **arm C** by one `clang-format` include reorder. It therefore predates every change
listed at the top of this file, including the guard, and was not re-run. Nothing in that diff alters
query results, plan shapes or explain output, but the gap is real and is stated rather than papered
over.
