# `get_children` — expand one node's children

**What MongoDB should change to serve this shape faster.** The workload is a JSON tree; this
operation expands one node's children, the single most common traversal step. Everything below is
scoped to changes in `mongod` or in PyMongo. Application-side workarounds appear only in §6, as
evidence bounding what a server change is worth.

**MongoDB 0.252 / 0.292 ms against PostgreSQL 0.100 / 0.113 ms (P50 / P95), 2.51×** — the widest
ratio of the four operations (`runs/all_ops_layouts_20260723/all_ops_10m.json`).

The query is a hinted `find` on `(tree_id, parent_id)` with a three-field projection, sorted
`(path, node_id)`, served by `PROJECTION_SIMPLE → FETCH → IXSCAN` — verified live, 11 returned,
11 keys examined, 11 documents examined, no rejected plans. The sort is satisfied by the index; there
is **no `SORT` stage**. PostgreSQL runs the equivalent against `allops_path_parent`, a btree on
`(tree_id, parent_id, path, node_id)`.

**The median node has 10 children** (cohort mean 10.10). The root-cause arms use a single parent with
a fan-out of **11**, just above the cohort median; the 64-parent cohort ranges 6–14.

Provenance. Server figures are CPU (`cpuNanos`), client figures are wall. The "client-side" row in §1
is `wall − server CPU` — a **residual**, not a measured quantity, and not comparable to either input.

---

## 1. Where the gap is

| | MongoDB | PostgreSQL prepared | gap |
|---|---|---|---|
| server CPU | 96.0 µs | 28.0 µs | 68.0 µs |
| client-side residual (wall − server CPU) | 100.0 µs | 52.0 µs | 48.0 µs |

Roughly **59% server, 41% client**.

## 2. This is a fixed per-command cost plus eleven cheap rows

The artifact contains a purpose-built arm for exactly this question: `get_children_miss`, the same
shape returning zero children, at **67.806 µs** (`mongo_cpu_arms_nolog.json`).

- **rows: 96.036 − 67.806 = 28.2 µs for 11 rows, 2.57 µs/row** — 29% of the operation.
- **fixed: 67.8 µs, 71% of the operation.**

The fixed cost is the same one `get_node` pays, and the artifact demonstrates that rather than
assuming it: `get_children_miss` 67.806 against `get_node_miss` 67.368, **0.65% apart** despite
different filters and different projection widths. Everything in `get_node.md` §2–§3 about that 71%
applies here unchanged.

The per-row side is closed. The retained profile gives `FetchStage::doWork` **14.38% / 13.8 µs** over
11 rows, i.e. **1.25 µs/row** of record fetch — about half the 2.57 µs/row total. There is no
concentrated per-row cost to attack.

## 3. Why PostgreSQL is ahead

Same mechanism as `get_node`. PostgreSQL's own per-phase CPU when it plans (`pg_cpu_arms.json`
`phase_split` — the psql/docker-exec instrument, usable for PostgreSQL's internal split but not for
cross-engine absolutes): PARSER 3, PARSE ANALYSIS 4, REWRITER 1, **PLANNER 22**, EXECUTOR 9 µs.
Planning is 22 of 39 µs.

PostgreSQL then stops paying it: psycopg3's `prepare_threshold` defaults to 5. (There is no prepared
phase split retained for `get_children`, so for this operation the inference follows from the
threshold rather than from a count.) On the right instrument, `pg_psycopg_cpu.json`, PostgreSQL's
server CPU is **28.0 µs prepared and 59.5 µs unprepared**, against MongoDB's 96.0.

MongoDB re-plans every call. Measured directly over 200 executions of this shape across 64 distinct
`parent_id`s, the classic cache records **0 hits / 200 misses** (`review_20260807/v3_sbe.json`).

PostgreSQL's EXECUTOR phase is larger in total here than for `get_node` (9 µs against 4), the
expected shape for eleven rows. Per row that is 0.8 µs — its executor is *cheaper* per row than for a
single-row lookup.

---

## 4. What MongoDB should change

### M1 — Cache plans for hinted single-solution classic queries · `mongod` · ≤11% of server CPU

> **Superseded by a direct measurement on master. See
> [`get_children_leads.md`](get_children_leads.md) L2d.** The ≤11% below is *derived* from a 7.0.34
> profile. Built and measured on master, the ceiling is **≈−10.3% of server CPU** (three campaigns,
> −10.29% / −11.58% / −10.33%, gate rotated across three servers, control floor −0.59% / +0.35% /
> −1.28%, two of three rotations improving in every block). **That is an upper bound**, and a
> correct implementation lands below it: the probe pays none of the real cache's key comparison,
> LRU and works-state bookkeeping on hit, none of the synthetic ranking decision on store, and none
> of the wider `encodeClassic` that every query on the server would pay once the hint is in the key.
>
> Two structural findings change what M1 *is*. Relaxing the `shouldCacheQuery` hint test alone would
> produce **zero stores and zero hits**: every path into the classic cache runs off a
> `MultiPlanStage` pick-best-plan callback, and a hinted query never multi-plans. And
> `encodeClassic` encodes neither the hint **nor `min`, `max` or tailable** — which is why
> `shouldCacheQuery` excludes all of them — so relaxing the exclusion without extending the encoder
> would let queries that differ in ways the key cannot see share one entry. M1 is therefore a
> substantially larger change than "flip a predicate", for about a tenth of the operation at best.
> **Lead closed, not deferred.**

**`get_children` is where this is best established of the four operations.** Under `trySbeEngine`,
which caches these shapes, server CPU falls across three retained runs:

| artifact | blocks | effect | block range |
|---|---|---|---|
| `opwin_20260807/framework_nolog.json` | 8 | −14.68% | [−26.12, +11.34] |
| `opwin_20260807/framework_nolog_r2.json` | 14 | −13.87% | [−36.61, −5.75] |
| `review_20260807/v3_sbe.json` | 12 | −15.98% | [−24.73, −4.58] |

Correctness: **0 mismatches across 64 inputs**, covering 1,971 returned elements. p99 improves 14.0%.
Cache counters under `trySbeEngine` are 199 hits / 1 miss over 200 executions against classic's
0 hits / 200 misses.

**What this does not establish.** Switching engines is worth 14–16%; **plan caching is bounded near
11%**. The retained profile for this exact operation
(`perf_nolog/get_children_hit.sym.inclusive.txt`, 92,519 samples) puts `QueryPlanner::plan` at
**11.35%** and all of `getExecutorFind` at 18.62%; a cache hit removes planning but still builds an
executor. The residual is the SBE runtime, and the cold-start cost is direct evidence the arms differ
in more than cache state — SBE is **+73.8 µs** on the first execution after `planCacheClear` because
it compiles a slot-based plan classic never builds. The *query plan* is identical under both engines
— same `PROJECTION_SIMPLE`/`FETCH`/`IXSCAN`, same index, 11 keys and 11 documents examined — but the
*execution engine* is not (`plan_explainer_sbe.cpp:370`).

Cold cost, `v3_sbe.json` `cold_children_first5_us` (client wall): classic
`[122.5, 122.7, 118.0, 122.8, 122.0]`, SBE `[196.3, 134.8, 114.1, 117.0, 114.4]`. SBE is 85.9 µs
behind after five executions — break-even at ~7 further executions against the paired steady-state
wall saving of 17.02 µs, or ~13 against the cold probe's own steady saving. The knob is
server-global; the other operations were checked and none regresses (`get_subtree` −1.39% server CPU,
0 mismatches over 98,922 rows; `get_entity` −0.05%).

**Making classic cache these shapes is the narrower change and the one worth proposing.**
`forceClassicEngine` is the deliberate 7.0.34 default, so defaulting to SBE is a deployment-policy
decision this evidence does not settle.

### M2 — Extend the fast path to cover a bounded prefix scan · `mongod` · **envelope now measured: ≈24 µs**

> **Update.** M2 was recorded below with no value. There is now a measured envelope for it, from a
> different shape: on master, `find({_id: X})` takes the express fast path while the same query with
> `hint: {_id: 1}` does not, so the two differ exactly by plan selection and executor construction
> over the same index for the same document. Express is **44.05% cheaper — about 24 µs per command**
> (+79.23% median for the hinted arm, blocks [+66.80, +92.21], against a same-process control floor
> of −0.44%).
>
> That 24 µs is fixed per-command machinery, which the two zero-row arms in §2 license treating as
> shared across shapes. Against this operation's ≈100 µs of server CPU on the same build it is an
> envelope of **≈24%**, of which M1 has now been shown to reach ≈8.6 points. **These are bounds
> derived from a different shape, not measurements of this operation** — a bounded-scan fast path
> must additionally iterate and satisfy the sort, so it would recover less than 24 µs, never more.
> Detail in [`get_children_leads.md`](get_children_leads.md) L4a. **This is the lead worth building.**

`get_node.md` §5 M1 proposes a fast path for unique compound-index equality. `get_children` is the
natural next shape: an equality on a *prefix* of a compound index (`tree_id, parent_id`) returning
a small bounded set, with the remaining index fields providing the sort. It is not a point query, so
the express machinery as it exists — which cannot iterate — does not apply without extension.

**No measurement supports a number here.** What is known is the size of the prize: 71% of this
operation is fixed per-command cost, of which planning is 11.35% and dispatch, acquisition and
transport are most of the rest. A fast path that skipped plan selection and executor construction for
a prefix-bounded scan would attack the same term M1 attacks on `get_node`, but nothing in this
evidence bounds it. Recorded as a direction, explicitly without a value.

### M3 — Pool-checkout fast path · PyMongo · 27.3 µs per command **[measured on `get_node`]**

Server selection, pool checkout and checkin on every operation against an already-pooled connection
and an unchanged topology. The ceiling is from the `get_node` ladder
(`pymongo_20260807/ladder.json`); it is per command rather than per row, so it should transfer, and
`opwin/driver.json` independently gives 63.4 µs for this operation's whole driver term against
`get_node`'s 62.9. **No ladder was run on this operation's shape.**

### M4 — Skip `Cursor` construction for single-batch replies · PyMongo · 24.4 µs **[measured on `get_node`]**

The eleven-row reply arrives with `cursor.id == 0` — it always fits under the 101-document default
first batch — so the `Cursor` is constructed, iterated eleven times and torn down for a result that
was complete on arrival. The 24.4 µs saving is a one-row measurement; here the driver additionally
runs eleven `__next__` iterations the measured arm does not, so the saving is not established at this
shape and could be larger or smaller.

---

## 5. Ruled out, with numbers

| | measured |
|---|---|
| the per-row path | 2.57 µs/row total, of which record fetch is 1.25. No concentrated cost |
| dropping the explicit sort | −4.33% (6/8 blocks) and −5.02% (13/14) server CPU, 19 of 22 overall — **but both per-block ranges straddle zero** ([−14.92, +11.52] and [−30.21, +2.93]). Bound at ≤5%, do not claim it |
| dropping the pinned hint | **+0.30% client-side P50**, s.e. 0.191, **17/40 blocks** — neutral to negative. A wall figure; no server-CPU nohint arm exists |
| a covering index | ceiling −24.42 µs (−25.5%), but a real covering index must carry `title` and `summary` in the key and would grow from `allops_tree_parent_path`'s 267 MB toward the 4.66 GB of the existing four-field cover index |
| larger `batchSize` | inert; the reply already arrives with `cursor.id == 0` |
| removing the container relay | engine-independent 20–25 µs per round trip; removing it from both sides widens the gap |
| `RawBSONDocument` | 2.4× slower — **measured on `get_subtree`'s 96,238-row shape**, and the mechanism scales with rows. **[unretained for this operation]** |
| the IXSCAN key-exclusion patch | **no established win.** Its own README opens "implemented and correct, but with no established performance win. Not proposed" — one build pair gave −2.01% on a C++ storage microbenchmark, a second from identical source gave +0.63% |

---

## 6. Application-side workarounds — evidence, not recommendations

- **Coalescing known parents into one `$in`**: 6.0× at B=64, ~2.22× at B=3, logging-corrected
  (`report.tex:1103-1107`; the uncorrected values 6.81× and 2.27× at `:1234` overstate by 7–12%).
  Markedly smaller than `get_node`'s 21.4×, because more of this operation's cost is per row and
  per-row work does not amortize — which is itself confirmation that the fixed cost is what M1, M3
  and M4 should attack.
- **Embedding children in the parent document.** Composed from an `IDHACK` point read at 45.7 µs plus
  a payload increment of 3.69 µs for ~11 children: −49.8% against the `opwin` baseline of 98.5 µs,
  −48.5% against this document's 96.0. **Not established.** It has had no independent review; the two
  terms come from different transports; the payload term is a single difference between two
  9,625-document probes at 1/1000 the real scale; and the composition assumes the `IDHACK` term is
  unaffected by the document carrying eleven children's `title` and `summary`, which was not tested.
  **[unretained at real scale]** Its costs — denormalization, write amplification on every child
  insert, the 16 MB fan-out cap — are real regardless. It is listed to record that the fixed cost can
  be avoided by restructuring, not to recommend restructuring.

---

## 7. Summary

`get_children` is the widest ratio of the four operations, and not because it returns rows: eleven
rows cost 28.2 µs of server CPU, 29% of the operation. The remaining 71% is the same fixed
per-command cost `get_node` pays — demonstrated, not assumed, by two zero-row arms agreeing within
0.65% — while PostgreSQL, having cached its plan after five executions, pays almost none of the
equivalent.

The MongoDB-side changes: **M1** cache plans for hinted single-solution classic shapes, worth ≤11% of
server CPU and better evidenced here than on any other operation; **M3** and **M4** in PyMongo, both
measured on `get_node` and expected to transfer; and **M2**, extending a fast path to a
prefix-bounded scan, recorded as a direction with no measured value. The per-row side is closed.
