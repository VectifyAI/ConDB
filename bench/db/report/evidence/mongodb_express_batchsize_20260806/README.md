# EXPRESS eligibility and `batchSize` — investigated, NOT proposed

**Status: blocked, not a candidate.** The effect is large and reproducible, but the change removes
behaviour that an existing core test deliberately depends on. It is recorded here rather than turned
into a pull request.

Base commit: `0561c098b99ac5e929005e70a2e37d7a97a82423` (see `base_commit.txt`). Local branch
`agent/condb-express-batchsize` in the `carsontung666/mongo` clone. Nothing was pushed.

## What was changed

`isExpressEligible` in `src/mongo/db/query/query_utils.h` rejects a query outright if
`findCommandReq.getBatchSize()` is set at all:

```cpp
if (!coll || findCommandReq.getReturnKey() || findCommandReq.getBatchSize() || ...)
    return ExpressEligibility::Ineligible;
```

An express plan returns at most one document and is exhausted once it has, so a *positive* batchSize
cannot change what it produces. `batchSize: 0` is different — it asks for a cursor with an empty
first batch, which would need the express executor to survive in a `ClientCursor`. The change was to
narrow the exclusion to that case only (`change.diff`), together with the benchmark support needed to
measure it.

## Effect: real and large

`point_query_bm.cpp` gained `UniqueFieldPointQueryWithBatchSize`, identical to the existing
`UniqueFieldPointQuery` except that it sets `batchSize: 1`. Both binaries built from the same tree,
run on one pinned CPU, 7 repetitions each, retired user-space instructions:

| Benchmark | Base | Patched | Ratio |
|---|---|---|---|
| `UniqueFieldPointQuery` (no batchSize — negative control) | 106,227 | 106,300 | 1.0007 |
| `UniqueFieldPointQueryWithBatchSize` | 171,098 | 106,490 | **0.6224** |

**37.8% fewer instructions.** Within-arm RSD is 0.04–0.21%, so the effect is roughly 180× the noise.
Reversing the arm order reproduces it (0.6247, `pq2_*.json`). The mechanism is visible in the
numbers: the patched batchSize case (106,490) lands on the same cost as the no-batchSize case
(106,300), because it now takes the same express path. The negative control confirms the already-
express path is unaffected.

## Behaviour: results identical, cursor lifecycle and diagnostics are not

`express_parity.js` captures the full wire reply and plan shape for 19 find shapes against both
binaries (`parity-base.out`, `parity-patched.out`). Document sets are identical in every case, and
the shapes that must not become eligible stayed ineligible: `batchSize: 0`, sort, `returnKey`, and
non-unique multi-match all keep their original plans.

Two differences did appear:

1. **Cursor lifecycle.** For `find({uniqueField: 7}, batchSize: 1)` — batchSize equal to the number
   of matching documents — base returns a non-zero cursor id and the client must issue a `getMore`
   that returns nothing; patched returns cursor id 0. The documents are the same. This is a strict
   improvement in round trips, but it is wire-visible.
2. **Plan summary and profiler output.** Queries that become express-eligible report
   `EXPRESS_IXSCAN` instead of `IXSCAN`/`FETCH`/`IDHACK`.

## Why this is blocked

`jstests/core/administrative/profile/profile_find.js:50` reads:

```js
// Use batchSize to avoid express path.
assert.eq(coll.find({a: 1}).collation({locale: "fr"}).limit(1).batchSize(2).itcount(), 1);
```

and then asserts `planSummary === "IXSCAN { a: 1 }"` along with the presence of `queryHash` and
`planCacheKey`. So the exclusion is **not** an unexplained oversight: at least one core test treats
`batchSize` as a documented escape hatch for forcing the non-express path, and depends on the
profiler output that follows from it. That test fails with this change
(`resmoke_with_new_test.json`).

Whether removing that escape hatch is acceptable — and what should happen to profiler and
slow-query-log output for the affected queries — is a call for the MongoDB query team, not something
local evidence can settle. Anyone picking this up must also reconcile the diagnostics contract, not
just the test.

## What else is unfinished

- A `jstests/core/index/express.js` addition written here asserts that `batchSize: 0` combined with
  `limit: 1` is not express, and that assertion fails. The likely cause is that the shell's
  `explain()` does not carry `batchSize` into the inner command, so the assertion tests shell
  plumbing rather than the server. It was not chased down.
- Before the new jstest was added, the six express and profile jstests passed 32/32 on the patched
  binary (`resmoke_before_new_test.json`). That run did not include `profile_find.js`'s failing
  assertion path, which only surfaced on the second run — treat the 32/32 as superseded by
  `resmoke_with_new_test.json`.
- No wider resmoke matrix was run, so the two failures found here are a lower bound on the fallout,
  not the full extent of it.

## Reproducing

```bash
# Two binaries from the same tree, differing only in query_utils.h.
taskset -c 0 ./point_query_bm \
  --benchmark_filter='^PointQueryBenchmark/UniqueFieldPointQuery(WithBatchSize)?/10/1024/threads:1$' \
  --benchmark_min_time=0.05 --benchmark_repetitions=7 --benchmark_report_aggregates_only=false

# Wire-level parity across 19 find shapes.
mongo --port <port> express_parity.js
```
