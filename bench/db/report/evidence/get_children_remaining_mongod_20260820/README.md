# get_children: no further mongod change clears the bar after express prefix scan

- status: investigated 2026-08-20, closed with a negative result
- scope: mongod only, `get_children` after `internalQueryEnableExpressPrefixScan`
- outcome: nothing further to build. No leftover mongod lead clears ~10% of
  remaining server CPU or tens of µs. No new experiment is warranted.

After express prefix scan, remaining mongod CPU is about 60 µs of command-path
tax plus about 6 µs of `findDoc`. No further mongod change for this operation
clears the bar.

## Absolute remaining

L10 shipping binary, three campaigns of 20 blocks, gate rotated
(`bench/db/runs/getchildren_plancache_20260810/dedup_rot{0,1,2}.json`):

| rotation | baseline median µs | probe (express) median µs | paired delta |
|---|---:|---:|---:|
| rot0 | 95.05 | 60.73 | −36.40% |
| rot1 | 97.03 | 60.99 | −37.21% |
| rot2 | 94.00 | 59.78 | −36.48% |

Campaign medians are **94–97 µs → 60–61 µs**. The remaining denominator used
below is **60.5 µs** (midpoint of the three probe medians 60.73 / 60.99 / 59.78).
Ten percent of remaining is **6.1 µs**. That is the bar.

`get_children.md` M2 quotes the same 60–61 µs campaign-median envelope.

## Gated-path breakdown

Source: `bench/db/runs/getchildren_plancache_20260809/perf_express/children.inclusive.txt`
(and exclusive). Knob on: `perf_express/mongod.log` line 14,
`internalQueryEnableExpressPrefixScan: true`. Symbols are
`PlanExecutorExpress<…PrefixScanViaUserIndex<FetchFromCollectionCallback>…>`
and `makeExpressExecutorForPrefixScan`.

This is not L3. L3
(`runs/getchildren_plancache_20260809/perf/children.inclusive.txt`) is the
ungated classic plan (`QueryPlanner::plan`, `PlanExecutorImpl::getNextBatch`).
Those two symbols are **absent** from `perf_express/children.inclusive.txt`.
Percentages below are of the gated capture, scaled onto 60.5 µs. Inclusive
siblings must not be added.

| frame | inclusive | of 60.5 µs |
|---|---:|---:|
| `SessionWorkflow::Impl::_doOneIteration` | 92.66% | 56.1 |
| `SessionWorkflow::Impl::_dispatchWork` | 72.63% | 43.9 |
| `executeCommand` | 64.65% | 39.1 |
| `FindCmd::Invocation::run` | 57.58% | 34.8 |
| `PlanExecutor::getNextBatch` (express loop, not `Impl`) | 21.62% | 13.1 |
| `PlanExecutorExpress<PrefixScan…>::getNext` | 20.78% | 12.6 |
| `CollectionImpl::findDoc` | 9.81% | 5.9 |
| `getExecutorFind` | 6.90% | 4.2 |
| `tryExpress` | 6.41% | 3.9 |
| `parsed_find_command::parse` | 6.55% | 4.0 |
| `_sendResponse` | 13.59% | 8.2 |
| `_receiveRequest` | 5.71% | 3.5 |
| `acquireCollectionOrViewMaybeLockFree` | 5.04% | 3.0 |
| `CanonicalQuery::CanonicalQuery` | 3.81% | 2.3 |
| `computeQueryShapeHash` | 3.43% | 2.1 |
| `fillOutIndexEntries` | 2.92% | 1.8 |
| `PrefixScanViaUserIndex::openCursorAndSeek` | 3.85% | 2.3 |
| `ProjectionStageSimple::transform` | 2.90% | 1.8 |

Outside `FindCmd::run`: 92.66 − 57.58 = **35.08 points ≈ 21.2 µs**.

Exclusive leaves (`perf_express/children.exclusive.txt`): largest is
`__wt_row_search` **2.47%**, then tcmalloc 1.91%. No hotspot.

## Candidates

Denominator for every row: remaining **60.5 µs**. Bar: **6.1 µs / 10%**, or
tens of µs as a standalone prize.

| candidate | skip | of remaining | class |
|---|---|---:|---|
| Cheap catalog on the express hit (hinted name lookup instead of full `fillOutIndexEntries`) | 2.92% | 1.8 µs | **below-bar** |
| Skip `computeQueryShapeHash` for express (SERVER-102484; `_id` already skips) | 3.43% | 2.1 µs | **below-bar** |
| Both of the above together | 6.35% | 3.8 µs | **below-bar** |
| Covering prefix scan (`CreateDocumentFromIndexKey`, no `findDoc`) | 9.81% | 5.9 µs | **closed** — live projection `{node_id, title, summary}` is not covered by `allops_tree_parent_path`; a real cover grows 267 MB → 4.66 GB (`get_children.md` §5). Schema, out of scope. The −24.42 µs covering ceiling is `{node_id}` only, pre-express. |
| Hinted plan cache (M1) | planning | — | **closed** — ceiling ≈−10.3% of the *pre-express* op, real build below that; L6 +0.43% vs hinted |
| Merge `express-compound-equality` | late `makePlannerParams` | — | **closed** — prefix scan needs the full index list up front; compound-equality's win is delaying that build |
| SBE / block processing | stage tree | — | **closed** — express already deleted the stage tree; SBE never sees this shape; fan-out 10 |
| Session / dispatch / OpCtx rewrite | the 21 µs outside `FindCmd` | 21 µs | **closed** — only leftover term that is tens of µs, and it is the whole command protocol, not a `get_children` patch. Largest exclusive leaf 2.47%. |
| Allocator campaign | 8% class exclusive on L3; 1.91% leaf here | — | **below-bar** as a targeted change |
| Lift `batchSize` / getMore | ClientCursor | 0 on this op | **closed** — L10 `getmore` and `cursor.totalOpened` are 0 |

No leftover *clears the bar*. The cheap-catalog item was still implemented and
measured, because it is a real, local change. Result below.

## Tried: cheap catalog on the express hit

Code: `/tmp/mongo-getchildren` on `express-prefix-scan`, knob
`internalQueryExpressPrefixScanCheapCatalog`. Hinted prefix-scan finds look up
only the hinted index instead of `fillOutIndexEntries`. Internal finds never
take the path (a first version segfaulted in `LogicalSessionCacheReap` on
`config.transactions`).

A/B: prefix scan on for all three arms; only probe has the cheap catalog.
Synthetic 64×10 tree, same four index names as the real collection. Artifact
`bench/db/runs/getchildren_cheap_catalog_20260820.json`, 12 blocks.

| | median |
|---|---:|
| probe vs baseline | **−3.56%** |
| control vs baseline | −0.23% |
| baseline | 95.9 µs |
| probe | 92.6 µs |

That is about **3 µs**, matching the gated-profile `fillOutIndexEntries` 2.92%
(~1.8 µs) plus a little. Several blocks were noisy (probe +40.6% in block 5;
blocks 8–9 had 160–260 µs baselines). The median is the number. **Below-bar.
Not proposed.** No experiment is warranted beyond this trial.

Query-shape hash was not built: `computeQueryShapeHash` runs in
`parseQueryAndBeginOperation` before `tryExpress`. Skipping it needs a find
front-end change and drops `$queryStats`, same trade SERVER-102484 already
owns. Ceiling remains 3.43% / ~2.1 µs.

## What this does not say

The last eligibility and yield edits on `express-prefix-scan` were not
re-measured. That is a property of the *existing* patch, not a leftover
mechanism that could clear another 10%. Rebase and re-measure belong with
upstream submission, not with hunting a second mongod change.

Driver M3/M4 and a prepared-find protocol are out of scope here.
