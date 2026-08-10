# Compound-equality express: what it is worth on the server

Date: 2026-08-10
Branch: `express-compound-equality` in the `carsontung666/mongo` fork, on top of
mongodb/mongo `0561c098b9`.

## What the change does

MongoDB's express executor answers a point query without building a
`QuerySolution` or running the planner. It ships for `_id` point queries and for
a single-field equality that fully binds a unique index. A conjunction of
equalities -- `{tree_id: "t1", node_id: "n0100000"}` against a unique
`(tree_id, node_id)` index -- is not eligible, so it takes the regular planner.

That is exactly the shape of `get_node`, and of any collection keyed by a
natural `(tenant, id)` pair rather than by `_id`.

The change makes a conjunction of equalities eligible when it binds *every*
field of a unique index. Partial binding stays ineligible: uniqueness is a
property of the whole key, so a predicate leaving a field unbound may match more
than one document and express would return only the first.

## Headline

Two independent harnesses, each with the same predicate answered with and
without express, in one binary.

### A real mongod, 200,000 documents, 7-field projection

Server-side retired user instructions, 15 interleaved blocks of 20,000
operations each. Arms are rotated block to block; the interval is a paired
bootstrap over per-block differences.

| arm | plan | instr/op | µs/op |
|---|---|---|---|
| `hinted` -- `.hint(idx)`, what ConDB sends today | PROJECTION_SIMPLE/FETCH/IXSCAN | 319,213 | 155.2 |
| `cached` -- unhinted, express off (= stock master) | PROJECTION_SIMPLE/FETCH/IXSCAN | 311,234 | 147.6 |
| `express` -- unhinted, express on (**the change**) | EXPRESS_IXSCAN | **215,171** | **120.4** |

`cached` → `express`: **−30.9% instructions**, **−18.4% latency**.
15/15 blocks in the same direction, CI95 on the ratio [44.61%, 44.67%].

### MongoDB's own `point_query_bm`, 10 documents, no projection

Single-threaded, 7 repetitions, aggregates only.

| arm | instr/op | ns/op |
|---|---|---|
| `CompoundUniqueFieldPointQueryExpressDisabled` | 224,504 | 34,713 |
| `CompoundUniqueFieldPointQuery` (**the change**) | **112,541** | **16,153** |

**−49.9% instructions, −53.5% time.** stddev is 31 instructions on 112,541,
i.e. 0.03%.

The two harnesses disagree on magnitude (31% vs 50%) for a reason worth stating:
the work express removes is fixed per operation, while the storage work it does
not touch grows with the collection. 10 documents is MongoDB's own benchmark
configuration; 200,000 is the realistic operating point. Both are reported.

## Why the numbers can be believed

**Positive control.** Every comparison is run alongside the same ablation
applied to *single-field* express, which MongoDB already ships. If the harness
could not resolve a known win, it could not resolve a new one either.

| harness | shipped single-field express | this change (compound) |
|---|---|---|
| mongod, 200k docs | 284,811 → 193,681 (−32.0%) | 311,234 → 215,171 (−30.9%) |
| point_query_bm, 10 docs | 180,601 → 98,925 (−45.2%) | 224,504 → 112,541 (−49.9%) |

The new path lands within a few points of the shipped one in both harnesses.

**Layout is held fixed.** Nothing here compares two binaries. Build-to-build
code-layout noise on this workload was measured at 13% -- larger than most
effects worth reporting -- so every arm comes from one binary with the feature
switched by `internalQueryDisableCompoundFieldExpressExecutor`. With the knob
set, `tryExpress` skips the entire express block and calls `makePlannerParams`
directly, which is what stock does, so the knob-off arm is a fair stand-in for
master.

**The plans were checked, not assumed.** The harness aborts unless `explain`
confirms `hinted` and `cached` execute the identical plan on the identical
index, that the express arms take `EXPRESS_IXSCAN` on the same index, and that
the non-express arms do not. Plan cache counters are recorded per arm:
`hinted` 500 skipped / 500 ops, `cached` 500 hits / 500 ops.

## A prior null, and why it was wrong

An earlier round reported this change as a null: −0.31%, CI [−0.90, +0.92]. That
measurement was invalid. The `point_query_bm` ablation arms set the knob by
storing to the underlying atomic:

```cpp
internalQueryDisableSingleFieldExpressExecutor.store(true);
```

Express eligibility does not read that atomic. It reads the process-wide
`QueryKnobSnapshot`, which is rebuilt by the on-update hook that only
`ServerParameter::set()` fires. A bare store leaves the snapshot holding the old
value, so both arms ran express and the measurement compared express against
itself. The fix is `unittest::ServerParameterGuard`.

The calibration arm caught this and was misread. Its comment states the failure
condition -- "if this shows no effect either, the harness cannot resolve express
at this collection size and neither pair says anything about the compound path"
-- and it showed 180,601 vs 180,601 for a feature worth 45%. That should have
been read as a broken harness, not a worthless change.

## Correctness

All seven of MongoDB's express jstests pass against a mongod with the change
active, including `express_pbt.js`, the property-based test that generates
compound index specs:

```
express_and_idhack_with_implicit_conjunctive_id   ok
express_id_eq                                     ok
express                                           ok
express_pbt                                       ok
express_projection                                ok
express_write_explicit_coll_creation              ok
express_write                                     ok
```

`express_plan_test` passes.

The nine cases added to `express.js` are not vacuous: run against a server with
`internalQueryDisableCompoundFieldExpressExecutor: true`, `express.js` fails.

## Not pursued

Letting hinted queries use the plan cache. `shouldCacheQuery` returns false for
any query carrying a hint, on the stated grounds that hinted queries are not
multi-planned -- which overlooks that a cache hit also skips `QueryPlanner::plan()`
itself. Measured, that is worth **2.6%** (`hinted` → `cached`, 319,213 →
311,234), which does not justify changing plan cache semantics.

## Files

- `mongod_arms.json` -- 15-block, five-arm run on a real mongod (the headline)
- `mongod_arms_r1.json` -- earlier three-arm run, 12 blocks, before the
  positive control was added
- `point_query_bm_fixed.txt` -- benchmark output with the knob correctly applied
- `point_query_bm_broken_knob.txt` -- the same run with the bare-store knob,
  kept as the record of the invalid measurement
- Harness: `bench/db/bench_hint_plancache_instr.py`
