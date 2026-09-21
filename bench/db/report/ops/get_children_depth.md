# `get_children` with depth — it is BFS, and MongoDB never made a fast path for it

**Determination.** Expanding a node to depth *D* is repeated `get_children`. The walk is
**BFS**. MongoDB's only recursive operator, `$graphLookup`, is also BFS
(`GraphLookUpStage::doBreadthFirstSearch`). There is no native DFS. The path-range
`get_subtree` the report already measured is a different algorithm: one index interval
in DFS/preorder, with no parent-id walk.

**Why this is a new lead.** One-level `get_children` is closed after express prefix scan
(`get_children.md` M2, remaining mongod headroom closed 2026-08-20). Depth was never
priced. The public ConDB API's `get_subtree` / `expand` / `format_tree_view` default to
**depth 1 or 2**. Beam retrieval is a BFS that calls `get_children_many` on the frontier
each turn. The storage-level subtree benchmark is the other extreme: unbounded path
range, no depth filter. Those two are not the same operation.

MongoDB does not have a first-class tree walk on `find()`. `$graphLookup` is an
aggregation stage for general graphs. It was not built for `(tree_id, parent_id)`
prefix scans, it materialises the reachable set as an array on the start document
(16 MB cap), and it does not use express. That is the gap.

Artifact: `evidence/children_depth_20260920/children_depth_80k.json` (SHA-256
`e39ad51109c0f7f9a88cf642370753ac2bcc5eee71dffe8dc7c032d853b95229`). Stock MongoDB
**7.0.34**, 80,000-node tree, fan-out 6–14, 32 roots, 5 repeats, client wall, one
pinned connection. **0 set mismatches** on every arm at every depth.

---

## 1. What "expand with depth" actually is

| API | Depth | Algorithm today |
|---|---|---|
| `get_children(tree, node)` | 1 | `find({tree_id, parent_id})` on `allops_tree_parent_path` |
| `ContextTree.expand(..., depth=1)` | 1 | `get_subtree` with `max_depth` |
| `ConDB.get_subtree(..., depth=2)` | **2** | path range **plus** `depth <= root_depth + D` |
| `format_tree_view(..., depth=2)` | 2 | same |
| Beam retriever | 1 per turn | BFS frontier, `get_children_many` |
| Report `get_subtree` | unbounded | path range, no depth filter |

`TreeDB.get_subtree` (`contextdb/core/storage.py:223`) is the path+depth form:

```sql
path > P || '/' AND path < P || '/\x7f' AND depth <= root_depth + max_depth
```

Depth is **not** in the path index. The scan walks every descendant and residual-filters
the ones that are too deep.

A BFS of `get_children` only ever reads nodes inside the depth budget.

---

## 2. BFS, not DFS

Three independent readings agree.

**Application.** Beam keeps a frontier and expands it one level per turn
(`beam_retriever.py:73-76`). That is BFS. The LLM ranks between levels, so Beam cannot
push *D>1* into the server; `get_subtree(depth=2)` and `expand` can.

**MongoDB.** `$graphLookup` is implemented as a queue. The comment on
`src/mongo/db/exec/agg/graph_lookup_stage.h:99` is "Perform a breadth-first search of
the 'from' collection." `doBreadthFirstSearch` drains `_queue` one depth at a time and
builds `{connectToField: {$in: frontier}}` wrapped in a `$match` pipeline per level.
There is no DFS sibling.

**Measurement.** Client-side BFS and DFS over the same `get_children` primitive issue
**the same number of finds** — they differ only in order:

| depth | BFS scalar finds | DFS scalar finds | wall p50 (cohort) |
|---|---:|---:|---|
| 1 | 1 | 1 | 0.286 / 0.274 ms |
| 2 | 15 | 15 | 3.630 / 3.631 ms |
| 3 | 164 | 164 | 35.17 / 35.51 ms |
| unbounded | 16,138 | (same shape) | 306 / 303 ms |

DFS is not cheaper. It is the same adjacency walk in a different schedule. Path order
*looks* like DFS because a materialized path is lexicographic preorder, but that is a
range scan, not a recursive descent.

---

## 3. `$graphLookup` is the native BFS, and it is not a fast path

For descendants of `start` with `maxDepth = D-1` (so *D*=1 is direct children):

```js
{$match: {tree_id, node_id: start}}
{$graphLookup: {
  from: nodes,
  startWith: "$node_id",          // or the literal id
  connectFromField: "node_id",
  connectToField: "parent_id",
  maxDepth: D-1,
  restrictSearchWithMatch: {tree_id},
  as: "desc"
}}
```

What it does, from the 7.0.34 / master source:

1. BFS queue of frontier values, same-depth batching (`makeQueryFromQueue`).
2. A **new aggregation pipeline per level**, not a `find`.
3. Inner predicate `$and: [restrictSearchWithMatch, {parent_id: {$in: frontier}}]`.
   The equivalent `find` on this collection uses `allops_tree_parent_path` (14 keys / 14
   docs at level 0; 150 keys / 149 docs at level 1). Drop `tree_id` and the same `$in`
   is a **COLLSCAN of 80,000**. The restrict is load-bearing.
4. Every reached document is copied into `_visitedDocuments`. The result is an **array
   on the start document**. Unbounded expansion of a 16,137-node subtree was skipped
   in this run because that array can exceed 16 MB. `$unwind` can be absorbed and
   stream, but the default shape cannot.
5. Express never sees it. Inner pipelines are not in `opcounters.find` (depth 2:
   `find: 0`, `aggregate: 1`).

It is a general graph operator with a BFS schedule. It is not a tree prefix scan.

---

## 4. Numbers, stock 7.0.34

80,000 documents, indexes matching the report (`allops_tree_node`,
`allops_tree_parent_path`, `allops_tree_path`, `layout2_rootcause_exact_cover`).
Cohort of 32 internal nodes at depth 1–2. Client wall. The **first root** (`n000005`,
depth 1) has 16,137 descendants — that is the overscan case. Cohort medians mix in
shallower depth-2 roots.

### Cohort median wall, ms

| depth | median n | BFS scalar | BFS `$in`/level | DFS scalar | path+depth | `$graphLookup` |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 13 | **0.286** | 0.283 | 0.274 | 1.743 | 0.427 |
| 2 | 145 | 3.630 | 1.066 | 3.631 | 2.030 | **0.772** |
| 3 | 1,373 | 35.17 | 4.674 | 35.51 | **2.760** | 3.752 |
| unbounded | 1,415 | 306.2 | 5.584 | 303.4 | **1.713** | skipped (16 MB) |

### First root (16,137-descendant subtree) wall, ms

| depth | n | BFS scalar | BFS level | path+depth | `$graphLookup` |
|---|---:|---:|---:|---:|---:|
| 1 | 14 | **0.309** | 0.293 | **17.435** | 0.482 |
| 2 | 163 | 3.907 | 1.109 | **17.990** | **0.814** |
| 3 | 1,630 | 41.47 | 5.369 | 18.635 | 4.390 |
| unbounded | 16,137 | 3,389 | 52.15 | **16.262** | cannot |

Explain of path+depth at *D*=2 on that root: **16,137 keys examined, 163 returned,
overscan 99×.** `PROJECTION_SIMPLE → FETCH → IXSCAN` on
`layout2_rootcause_exact_cover`. Depth is a residual filter. The unbounded path scan
examines the same 16,137 and *keeps* them, which is why unbounded path is the cheap
one and depth-limited path is not.

Commands on that root: BFS/DFS scalar at *D*=2 is **15 finds**; BFS level is **2
finds**; path is **1 find**; `$graphLookup` is **1 aggregate**. Same sets.

---

## 5. Where the optimisation space is

One-level `get_children` remains closed. This lead is depth *D≥2*, and specifically
the API default *D*=2.

**Path+depth is the wrong algorithm for shallow expansion of a large subtree.**
On the 16,137-node first root, depth 1 is 17.4 ms vs 0.31 ms for `get_children`
(56×). Depth 2 is 18.0 ms vs 0.81 ms for `$graphLookup` (22×). The report's unbounded
path numbers do not apply here.

**`$graphLookup` already wins at D=2**, including against batched client BFS (0.77 vs
1.07 ms cohort; 0.81 vs 1.11 ms on the large root). It loses at D=1 to a single
`find` (0.43 vs 0.28 ms) — that is aggregation tax for one hop. It loses to path once
the depth budget covers most of the subtree (unbounded: path 16 ms vs BFS-level 52 ms
on the large root). It cannot return the large set as an array.

**DFS is not a separate prize.** Same find count as BFS. MongoDB has no DFS operator
to improve. Full-subtree DFS order is the path range, already measured.

**What a mongod change would skip**, if it is worth building:

1. **Pipeline-per-level.** `doBreadthFirstSearch` constructs a `$match` pipeline for
   every BFS layer. D=1 should be the express prefix scan we already have. D=2 should
   be two prefix scans (or one `$in`) without CanonicalQuery + planner + stage tree.
2. **Array materialisation.** Stream like `find` / absorbed `$unwind`. Retires the
   16 MB cap for this shape.
3. **FETCH of unused fields.** Same covering question as one-level `get_children`;
   still schema, still closed as a *server* change.

Ceiling vs today's `$graphLookup` is unmeasured in server CPU. The D=1 gap (0.43 vs
0.28 ms wall, ~50%) is an upper bound on "delete the aggregation wrapping of one
prefix scan". D=2 should be larger in absolute µs and smaller in percent. A fused
express BFS that also beats `$graphLookup` is the thing to price with a probe, the
same way L4a priced express before it was built.

**What would close this lead without mongod:** ConDB's MongoDB `get_subtree(depth=2)`
switching from path+depth to `$graphLookup` or batched BFS. That is an application
fix, 22× on the large root, and it does not produce a MongoDB commit. The mongod
credit is the missing fast path for the BFS `$graphLookup` already runs.

---

## 6. Built: covered tree-BFS `$graphLookup`

Branch `graphlookup-index-scan` on the local mongo tree. Gate
`internalQueryEnableGraphLookupIndexScan`, default false. The whole BFS is one
`graphLookupTreeBFS` call: one collection acquisition, one index cursor. When
`connectFromField` (`node_id`) is in the index, there is **no FETCH** — keys are
rehydrated in place. Results go to `_fastPathBson`, skipping
`SpillableDocumentMap`.

Same binary, gate off vs on, 80k-node tree, 16 roots, 5 repeats, client wall,
**0 set mismatches**. Artifact
`evidence/graphlookup_index_scan_20260920/graphlookup_covered_bfs_80k.json`
(SHA-256 `24f91a686f0c469dcafe3d09181ecd812350badae4d46f0537a54a8a82f95917`).

### Gate off (before) vs gate on (after), cohort median wall ms

| depth | n | before `$graphLookup` | after covered BFS | vs before | path+depth |
|---|---:|---:|---:|---:|---:|
| 1 | 14 | 0.461 | **0.369** | **−19.8%** | 1.623 |
| 2 | 151 | 0.785 | **0.527** | **−32.8%** | 1.839 |
| 3 | 1,509 | 3.199 | **1.807** | **−43.5%** | 2.532 |

### First root (16k-descendant subtree)

| depth | n | before | after | vs before | path+depth |
|---|---:|---:|---:|---:|---:|
| 1 | 14 | 0.551 | **0.383** | −30.5% | 14.777 |
| 2 | 163 | 0.836 | **0.530** | **−37%** | 14.285 |
| 3 | 1,630 | 3.744 | **2.019** | **−46%** | 15.342 |

Depth 3 is the largest win: skipping FETCH on ~1.5k documents is no longer
drowned by record load. Covered BFS also beats path+depth at D=3 on the cohort
(1.807 vs 2.532) and remains ~7× on the large root.

Superseded A/Bs of the same gate, weaker patches: per-`PlanExecutor`
(`graphlookup_index_scan_80k.json`, D=3 **+14%**) then cursor+FETCH
(`graphlookup_index_scan_cursor_80k.json`, D=2 −7.7%, D=3 +0.5%). Neither
covered; both still paid FETCH.

### After P0 correctness (2026-09-21)

Fast path is tree-only: visited-from-values de-dup, no FETCH fallback index,
covering emit only when `{$project:{_id:0, out:"$as.field"}}` is absorbed,
missing `startWith` and regex `restrictSearchWithMatch` refuse to stock.
Same binary, same 80k/16/5 harness, **0 set mismatches**. Artifact
`bench/db/runs/graphlookup_p0_correctness_80k.json` (SHA-256
`aca25d2f383c2f9883d79914dc0ac3e7c8dc2eea57d502f00e6fbf4333b687f8`).

| depth | n | before `$graphLookup` | after | vs before |
|---|---:|---:|---:|---:|
| 1 | 14 | 0.510 | **0.349** | **−31.5%** |
| 2 | 163 | 0.848 | **0.466** | **−45.0%** |
| 3 | 1,630 | 3.267 | **1.380** | **−57.7%** |

### Tree height is 5, not 3 (2026-09-21)

The 80k collection is a directory-shaped tree: fan-out 6–14, **max depth 5**,
86% of nodes at depth 5. Filesystem retrieval walks depth **4**; examples call
`get_subtree(..., max_depth=5)`. D=1..3 is only the public-API default, not the
tree.

From the real root `n000000`, same binary, 5 repeats, **0 mismatches**. Artifact
`evidence/graphlookup_index_scan_20260920/graphlookup_from_root_d1to5.json`
(SHA-256 `fc38c2b6a1f51fe83b093fdab831df1606b946717244ecd7f90f3e3081c653bb`).

| depth | n | before `$graphLookup` | after | vs before | path+depth |
|---|---:|---:|---:|---:|---:|
| 1 | 11 | 0.529 | **0.326** | **−38.4%** | 74.1 |
| 2 | 111 | 0.785 | **0.415** | **−47.2%** | 69.2 |
| 3 | 1,113 | 2.829 | **0.980** | **−65.4%** | 69.2 |
| **4** | **11,088** | **28.575** | **7.918** | **−72.3%** | 73.9 |
| **5 (whole tree)** | **79,999** | **1217** | **57.808** | **−95.3%** | 108.1 |

A 16-root cohort at D=4/D=5 only shows −12%/−13% because most of those nodes
already hit leaves at D=3 (median n stuck at 1,509). The large directory in that
cohort (16,137 descendants) is D=4 **−81.4%**, D=5 **−46.9%**. Artifact
`evidence/graphlookup_index_scan_20260920/graphlookup_depths_1to5.json`
(SHA-256 `f08d45387103dfa202e66098b9eb566b9631628156a74af18430358359b051b1`).

Not upstream. Not rebased onto the express-prefix-scan branch.

---

## 7. Limits

- 80k synthetic documents, not the 10M layout-2 collection. Overscan *ratio* is a
  property of subtree size vs depth, and the first root here is 16k descendants
  against the report's median 11.7k / mean 36k — same regime.
- Client wall, not server CPU. Directional; a mongod probe must use the connection-
  thread instrument.
- `$graphLookup` inner plans do not appear in aggregate `explain` or in
  `opcounters.find`. Index use was confirmed on the equivalent `find`, not on the
  stage's private pipeline.
- Beam cannot consume a server-side D>1 expansion without changing the ranking
  loop. The API methods can.
- No PostgreSQL recursive-CTE arm in this file. `optimization.tex` already has
  recursive adjacency 4× slower than typed path for *unbounded* descendant IDs;
  that does not refute BFS for *shallow* expansion.

Harness: `bench/db/bench_children_depth.py`.
