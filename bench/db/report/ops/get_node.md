# `get_node` — single node lookup

**What MongoDB should change to serve this shape faster.** The workload is a JSON tree; this
operation fetches one node by its natural key. Everything below is scoped to changes in `mongod` or
in PyMongo. Application-side workarounds appear only in §6, where they serve as evidence bounding
what a server change is worth — they are not recommendations.

**MongoDB 0.196 / 0.220 ms against PostgreSQL 0.092 / 0.107 ms (P50 / P95), 2.14×**
(`report.tex:356`).

The query is a hinted `find_one` on `(tree_id, node_id)` with a seven-field projection, served by
`PROJECTION_SIMPLE → FETCH → IXSCAN`. **`allops_tree_node` is a `unique` index on
`(tree_id, node_id)`** — verified live — so this is a point query that can match at most one
document, semantically identical to an `_id` lookup. PostgreSQL runs the equivalent `SELECT` against
`layout2_pg_view`, served by `sql_native_path_view_root`, a unique btree on the same two fields
`INCLUDE (path)`.

Provenance. Server figures are CPU (`cpuNanos`); client figures are wall. The decomposition arms
have **per-operation logging disabled** (`mongo_cpu_arms_nolog.log:4`), which is why their absolutes
sit below the headline table: `slowms=0` is worth **47.1 µs, 39.8%** of this operation
(`opwin_20260807/TABLES.txt` §2). The pinned hint is not the reason — it is worth 0.05 µs here
(`get_node_hit` 71.703 vs `get_node_hit_nohint` 71.649).

---

## 1. Where the gap is

| | MongoDB | PostgreSQL | gap |
|---|---|---|---|
| server CPU | 71.7 µs | 20.5 µs prepared / 46.5 µs unprepared | 51.2 / 25.2 µs |
| wall − server CPU | 93.8 µs | 50.4 µs | 43.4 µs |

Both PostgreSQL server figures come from one run and one instrument
(`bottleneck_20260806/pg_psycopg_cpu.json`, Python 3.10 arm).

**The second row is a residual, not a client-side cost, and about a fifth of it is neither
engine's.** `opwin_20260807/TABLES.txt` §0 decomposes the same operation as wall 166.9 = server CPU
71.6 + client CPU 75.5 + **19.8 µs unaccounted**, and states the remainder is not idle time: the
container relay is a third process whose CPU appears in neither term. §7 prices that relay at
19.5–22.4 µs for MongoDB and 24.2–25.2 for PostgreSQL. So roughly 20 µs of MongoDB's 93.8 and 24 µs
of PostgreSQL's 50.4 belongs to the harness.

## 2. MongoDB's 71.7 µs of server CPU

`runs/bottleneck_20260806/decomp_get_node/get_node_phases.txt`. This is an **exclusive** partition —
`phases.py` matches leaf-to-root, first hit wins — so the rows sum to 71.71 µs and 100.00% and no
frame is counted twice. Raw perf data retained in `perf_nolog/`.

| component | µs | share |
|---|---|---|
| transport | 19.79 | 27.61% |
| `getExecutorFind` — of which the `QueryPlanner::plan` leaf bucket 8.84 | 15.46 | 21.56% |
| command dispatch above `FindCmd::run` | 11.10 | 15.48% |
| execution | 6.03 | 8.40% |
| collection acquisition | 5.62 | 7.83% |
| projection AST parse and analysis | 4.22 | 5.89% |
| command parse + teardown | 4.13 | 5.76% |
| `find` setup, filter, post-processing, unattributed | 5.36 | 7.47% |

**Reading the data is 1.99 µs, 2.78%** — the non-nested `__wt_btcur_*` total in a tree cut at 0.15%.
The fair upper reading, all non-nested WiredTiger-or-wrapper frames, is **9.83%**. The storage engine
is not where this operation spends its time. The rest is a per-command path that does not depend on
the data, and it is re-walked on every call: hinted single-solution classic queries **always miss the
plan cache and never populate it** (`classic_plan_cache.cpp:141`; `plan_cache_per_shape_raw.txt`
records +100 misses, 0 hits). Unhinted, the same query carries a rejected plan and does populate the
cache, so the hint is what disqualifies it.

## 3. MongoDB's client-side cost

`runs/pymongo_fused_20260807/ladder.json`, harness `bench_pymongo_ladder.py`: five arms peeled one
layer at a time, 14 blocks × 500 iterations, rotated within each block, all verified to return
identical documents every block. CPU is `time.process_time()`.

| arm | wall µs | client CPU µs | CPU removed by this step |
|---|---|---|---|
| `find_one` — public API, builds a `Cursor` | 216.4 | 79.1 | — |
| `Database.command` — byte-identical wire, no `Cursor` | 191.4 | 54.8 | **24.4** |
| `Connection.command` with session and client, connection held | 167.7 | 27.4 | **27.3** |
| `Connection.command` with neither | 163.3 | 24.6 | **2.9** |
| hand-written `OP_MSG` on the same socket | 152.6 | 16.9 | **7.7** |

**62.2 µs of client CPU per command is driver overhead the wire exchange does not require**,
corroborated at 62.98 µs by a different harness and run (`review_20260807/v2_driver_paired.json`).
Composition: **27.3 µs (44%)** per-operation pool checkout, checkin and server selection; **24.4 µs
(39%)** the `Cursor` layer; **7.7 µs (12%)** remaining command machinery; **2.9 µs (5%)** session
application and cluster-time gossip. The `conn_sess` arm exists to separate that last item, because a
supported fast path cannot drop the implicit session.

The **wall** column at the `command → conn_sess` boundary is not usable for attribution: holding one
connection means the upper arms run on a different socket, and `connection_lottery_20.json` records a
13.9% spread across fresh connections. The client-CPU column is unaffected.

Two candidate explanations were tested and both are dead. **Syscalls are not the cost**: `strace -c`
over 1,300 lookups gives 1 `sendto`, 2 `recvfrom`, 2 `poll` per operation with `setsockopt` and
`fcntl` appearing ~12 times *in total*, so an earlier claim of "four `settimeout` syscalls per
operation" was false; fusing the header and body reads measured **−0.50%, 7/12 blocks**, inside noise
(`get_node_8k.json`). **There is no hot spot**: `cprofile.json` shows `find_one` spending 1.066 s
across **240 distinct functions** over 3,000 calls, largest non-socket frame 0.026 s
(`cursor.py:96 __init__`) — wall times inflated by the profiler, establishing shape not magnitude.

## 4. Why PostgreSQL is ahead

PostgreSQL's own per-phase CPU when it plans — PARSER 3, PARSE ANALYSIS 4, REWRITER 1, **PLANNER
17**, EXECUTOR 4 µs (`pg_cpu_arms.json` `phase_split`). That is a **different instrument** from every
other PostgreSQL number here: `psql` through `docker exec` over a container-local socket
(`bench_bottleneck_pg_cpu.py:21`), so its totals are not comparable to the psycopg figures and
"17 of 29" is a share within that instrument only. Against the psycopg unprepared total of 46.5 µs
the planner term is 37%.

**On the matched arm MongoDB's server-side deficit is 25 µs, not 51.** MongoDB re-plans on every
call, so PostgreSQL *unprepared* — 46.5 µs — is the like-for-like comparison. The 51.2 µs figure
compares against a PostgreSQL arm that has stopped planning, which is what the benchmark measures
because `bench_all_ops_layouts.py` drives psycopg3 with `prepare_threshold=5`. Both are legitimate
answers to different questions.

An earlier version cited "the planner ran 6 times in 400 executions" as evidence of psycopg3's
threshold. **That inference does not hold**: the 6 is from the psql arm, which issues an explicit
`PREPARE`/`EXECUTE` and was never psycopg3, and 6 is PostgreSQL's own `plan_cache_mode=auto`
behaviour — one plan at `PREPARE` plus five custom plans — coinciding with `prepare_threshold=5` by
accident. The prepared/unprepared pair is the evidence; the counter is not.

---

## 5. What MongoDB should change

### M1 — A fast path for unique compound-index equality · `mongod` · ~26 µs, 36% of server CPU

**This is the largest server-side item on this operation, and it is a JSON-tree-shaped gap.** Tree
nodes are naturally keyed by `(tree_id, node_id)`, not by `_id`. MongoDB gives an `_id` equality a
fast path — `IDHACK` on 7.0.34, `EXPRESS` on 8.0+ — that skips plan selection and executor
construction entirely. It gives the *semantically identical* lookup on a unique compound index
nothing: `allops_tree_node` is unique and both its fields are bound by equalities, so the seek can
match at most one key, yet the query pays full planning on every call.

Measured value of the fast path: an `_id` equality without a hint costs **−36.7% server CPU**
(block range [−40.3, −29.9], 14/14 blocks) and **−17.4% wall** (14/14) against this same query
(`review_20260807/v6_idhack.json`) — **26.3 µs** of the 71.7. That figure measures the fast path on
an `_id` lookup; transferring it to a compound-key eligibility rule assumes the work saved is the
same, which is true of plan selection and executor construction but has not been measured on a
compound-key implementation.

**An implementation exists and is unverified.** The fork carries
`agent/condb-express-compound-eq`, commit `4fb23d8d1ba` "Extend the express fast path to compound
equality predicates", one commit ahead of `upstream/master`. It generalises
`collectExpressEqualities()` to admit a conjunction of equalities on distinct paths, and
`orderEqualitiesForIndex()` to line those operands up against a candidate index, rejecting it unless
the index's leading fields are exactly the constrained paths. It has **not** been through this
project's acceptance gate — no independent review, no proof of effect on the real dataset, no proof
of non-intrusion — and none of the numbers above were measured against it.

Two constraints that must survive into any implementation, both verified live on 7.0.34:

- **A hint disqualifies the fast path** (`query_utils.cpp:52-59` requires an empty hint). Production
  tree workloads pin hints for plan stability, so a fast path that a hint disables is unavailable
  exactly where plan stability matters. Whether the hint check can be relaxed when the hinted index
  *is* the index the fast path would choose is an open question this evidence does not settle.
- **A sub-document `_id` matches by exact document equality including field order** —
  `{_id: {tree_id, node_id}}` returns one document and `{_id: {node_id, tree_id}}` returns none,
  both taking `IDHACK`, no error, identical plan. A compound-key fast path avoids this hazard
  entirely, which is a correctness argument for M1 independent of its performance argument.

### M2 — Cache plans for hinted single-solution classic queries · `mongod` · ≤12% of server CPU

Today these shapes always miss and never populate (`classic_plan_cache.cpp:141`, `shouldCacheQuery`
returns false when a hint is present and the query is not SBE-compatible; 7.0.34 defaults to
`forceClassicEngine`, `query_knobs.idl:881-888`).

Evidence, obtained by switching engines rather than by patching the cache. Under `trySbeEngine`,
which does cache these shapes, the SBE cache records 199 hits / 1 miss over 200 executions where
classic records 0 hits / 200 misses. Effect on `get_node` server CPU across three retained runs:

| artifact | blocks | effect | block range |
|---|---|---|---|
| `opwin_20260807/framework_nolog.json` | 8 | −7.57% | [−14.5, −1.2] |
| `review_20260807/v3_sbe.json` | 12 | −11.07% | 10/12 blocks |
| `opwin_20260807/framework_nolog_r2.json` | 14 | −12.00% | **[−50.6, +20.0]** |

Envelope 7.6–12.0%, with the largest point estimate's interval straddling zero. The *query plan* is
identical and results byte-equal, but the *execution engine* is not — under `trySbeEngine` the
`winningPlan` carries a slot-based tree (`plan_explainer_sbe.cpp:370`) — so this measures switching
engines, not caching. **Caching is bounded near 12%**, the `QueryPlanner::plan` leaf bucket being
8.84 µs of 71.7; the measured range sits under that bound. On `get_children` the same experiment
gives 14–16% against an 11.35% planning term, where the residual is demonstrably the SBE runtime.

Cold cost is **[unretained for this operation]**: the retained cold series is `get_children` and it
is client wall, not server CPU. Making classic cache these shapes is the narrower change; defaulting
to SBE is a deployment-policy decision no measurement here settles.

**M1 and M2 do not compose** — an `IDHACK`-eligible query is by construction not pushed to SBE
(`query_utils.cpp:61-70`), and a fast-path query does no planning to cache.

### M3 — Pool-checkout fast path · PyMongo · 27.3 µs per command, 44% of the driver cost

Server selection, pool checkout and checkin run on every operation against an already-pooled
connection and an unchanged topology. Nothing has been attempted. The obvious shape is a fast path
for the common case — one server, healthy checked-out connection — with session application and
cluster-time gossip (2.9 µs, measured separately by the `conn_sess` arm) deliberately left inside
the supported path. Ceiling 27.3 µs.

### M4 — Skip `Cursor` construction for single-batch replies · PyMongo · 24.4 µs per command

`find_one` and `Database.command` put **byte-identical** documents on the wire, yet `command` costs
24.4 µs less client CPU. Paired 16 blocks (`review_20260807/v2_driver_paired.json`): −23.46 µs
paired median, 16/16 blocks; wall −12.8%, 15/16; server CPU −1.25 µs, 9/16 — within spread. So the
saving is not the retry machinery (2.6 µs) and not the wire: it is `Cursor` object construction and
teardown for a reply that has `cursor.id == 0` and will never be iterated.

`find_one` already sets `limit:1`/`singleBatch`, so the driver knows before it sends that the reply
cannot need a cursor. Recognising that and returning the document directly is a driver-internal
change requiring no API change and no user action.

---

## 6. Application-side workarounds — evidence, not recommendations

These bound what the server changes are worth. They are recorded because they were measured, not
because a MongoDB user should have to do them.

- **Moving the natural key into `_id`** buys the −36.7% above. It is what M1 makes unnecessary. Its
  cost to the application is a schema migration and, if done as a sub-document, the silent
  field-order hazard in §5 M1. If done as a string it also loses the ability to query the components
  independently.
- **Calling `Database.command` instead of `find_one`** buys the 24.4 µs of M4 today, but only when
  the reply's cursor id is zero; otherwise the caller silently leaks a server-side cursor for
  `cursorTimeoutMillis`, default ten minutes. It also moves read preference, read concern and
  retryable reads to the caller. This is a workaround for a driver defect, not a usage pattern to
  recommend.
- **Coalescing known ids into one `$in`**: 21.4× at B=64, ~2.6× at B=3, logging-corrected
  (`report.tex:1104-1106`; the uncorrected table values 24.1× and 2.66× at `:1204-1205` overstate by
  7–12%). This works because the cost is per command — the same reason M3 and M4 exist — so it is
  best read as independent confirmation that the per-command fixed cost dominates this operation.

---

## 7. Ruled out, with numbers

| | measured |
|---|---|
| dropping the pinned hint | **−1.29%**, s.e. 0.116, 40/40 blocks. The report's "19 µs, ~9%" figure is `_id`-specific; there is no `IDHACK` here for a hint to suppress — verified live, hinted and unhinted both plan `PROJECTION_SIMPLE → FETCH → IXSCAN` with the same `planCacheKey` |
| SERVER-13341 as written | **0 µs** — the cache is never populated for this shape. What the ticket proposes is not recorded in this tree; this is an inference from cache behaviour, not from the ticket text |
| `fillOutIndexEntries` | ~0 — 2 / 5 / 8 indexes give 71.33 / 71.42 / 70.69 µs |
| `EXPRESS` as it exists upstream | absent from 7.0.34 entirely. On `upstream/master` `collectExpressEqualities` requires a single `EQ`, so this shape falls through to regular planning — which is precisely the gap M1 addresses |
| fusing the driver's header and body reads | −0.50%, inside noise |
| removing the container relay | paired medians: MongoDB 19.5–22.4 µs, PostgreSQL 24.2–25.2 µs per round trip. Because PostgreSQL's operations are shorter, removing it from **both** sides *widens* the gap — 3.7 µs for `get_node` |
| `directConnection`, `retryReads=false`, `heartbeatFrequencyMS`, client NUMA pinning | d50 −1.17% to +0.67% with inconsistent signs, against `sem50` of 1.0–1.9% and null controls of −0.24% / +0.41% / +0.56% |

---

## 8. Summary

`get_node` is not slow because of storage, indexing or data volume — reading the data is 2.78% of its
server CPU. It is slow because a fixed per-command path is walked twice, and MongoDB has a fast path
for exactly this kind of lookup that it declines to offer here.

The four MongoDB-side changes, ranked: **M1** give unique compound-index equality the fast path
`_id` already gets (~26 µs, 36% of server CPU; an unverified implementation exists on
`agent/condb-express-compound-eq`); **M3** a pool-checkout fast path in PyMongo (27.3 µs); **M4**
skip `Cursor` construction for single-batch replies (24.4 µs); **M2** cache plans for hinted
single-solution classic shapes (≤12%). M1 and M2 do not compose. Together M1, M3 and M4 address
roughly 78 µs against a total gap of 104 µs.
