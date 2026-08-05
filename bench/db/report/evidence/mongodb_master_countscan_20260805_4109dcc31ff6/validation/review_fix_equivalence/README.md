# The reviewed commit versus the measured commit

The 450-process campaign measured arm C = `90814b83d3e55f099c1244266d86700b5f633972`. A later
blank review of the change as a MongoDB reviewer would read it produced fixes, and the commit now
under review is `90781b36b2` (see `reviewed_commit.txt`). This directory shows that those fixes did
not move the measured quantity.

`measured_to_reviewed.diff` is the complete difference. It consists of:

- the `PlanStage::work()` contract comment, rewritten from a general permission into a closed
  exception naming `CountScan`, because as written it licensed any stage to return `ADVANCED`
  without a valid id;
- a `tassert` in the setter that the scan has not yet been worked. `ClassicPlannerInterface::makeExecutor()`
  wraps rejected plan roots in a fresh `CountStage` when explaining a count, which is a second,
  previously unremarked call site; the assertion makes the ordering assumption enforced instead of
  argued;
- `setDoesNotMaterializeResults` renamed to `disableResultMaterialization`, and the flag moved next
  to `_shouldDedup` so it packs into existing padding rather than costing 8 bytes;
- the class docstring, which claimed unconditional `RID_AND_OBJ` materialization, and a comment on
  the member itself;
- test cleanups: a redundant `static_cast`, a `make_unique`/`release` round trip, one signed
  literal, and a docstring correction — `QueryStageCountScanDirectCountStageSemantics` is an
  invariance test that passes with and without the change, and now says so;
- `count_query_bm.cpp` and the `QueryBenchmarkFixture` helpers it needs, so the benchmark that
  produced the campaign numbers ships in tree.

Nothing in that list executes inside `CountScan::doWork()`'s per-key path. The `tassert` runs once
per `CountStage` construction.

## Measured check

The reviewed binary was smoked at campaign size on the same pinned CPU with the same command shape
the campaign used (`taskset -c 0`, `--benchmark_min_time=0.01`, `--benchmark_repetitions=5`). The
JSONs are beside this file.

| Workload | Reviewed commit | Campaign arm C, mean of 30 | Ratio | Arm C spread over its 30 processes |
|---|---|---|---|---|
| Scalar, 400k | 978,969,359 | 978,986,595 | 0.999982 | 0.0093% |
| Multikey, 200k | 1,106,166,214 | 1,106,058,624 | 1.000097 | 0.0168% |
| Wildcard, 200k | 612,750,402 | 612,754,586 | 0.999993 | 0.0150% |

Each difference is smaller than arm C's own process-to-process spread in the campaign, so the
reviewed commit is not distinguishable from the measured one on any count endpoint.

The fetching-count control is deliberately omitted from that table. It carries the per-process
two-state instruction latch described in the report, so a single process cannot be compared against
a 30-process mean that averages both states. The smoke value, 1,875,900,909, sits in the lower
state, whose campaign mean is 1,875,801,579 — a ratio of 1.000053, again inside the within-state
spread. `smoke_fixed_X.json` is retained so this can be checked.

This is a smoke comparison, not a repeat of the campaign. It shows the effect transfers; it does not
re-establish the intervals, which remain pinned to `90814b83d3e5`.

## Behavioural checks on the reviewed commit

Both count dbtest suites pass on the reviewed commit: `query_stage_count_scan` 14/14 and
`query_stage_count` 7/7.

Against unpatched base `0561c098b99a`, with the four production files reverted and the tests kept,
the suite reports **874 TOTAL / 13 PASS / 1 FAIL**. The single failure is
`QueryStageCountScanMaterializationContract`, which is the test that observes the change.
`QueryStageCountScanDirectCountStageSemantics` passes on base, as it must — every assertion in it is
derived from `StageState`, which the change does not touch.

The resmoke and explain-parity evidence in the parent directory was gathered before these fixes and
was not re-run. Nothing in the diff alters query results, plan shapes or explain output.
