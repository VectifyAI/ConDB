# Optimize MongoDB for `get_children` — expand one node's children

You own this operation end to end. MongoDB is paying for this work; every deliverable is a change to
MongoDB's own code. **This document gives you an initial plan. Execute it first. If its measured
effect is insufficient, do not stop — continue down the fallback leads, and past them if necessary,
under the same discipline.**

Three sibling agents work the other operations on this box. Your lane is the **plan cache**
(`classic_plan_cache.cpp` and the cached-plan execution path). The express/fast-path eligibility
code belongs to the `get_node` agent, the driver to the `get_entity` agent, cursor batching to the
`get_subtree` agent — do not modify those areas, and coordinate builds (see Discipline).

## The operation and the gap

Hinted `find` on `(tree_id, parent_id)`, three-field projection, sorted `(path, node_id)` —
`PROJECTION_SIMPLE → FETCH → IXSCAN`, no SORT stage, median 10 children. **MongoDB 0.252 ms vs
PostgreSQL 0.100 ms P50, 2.51× — the widest ratio of the four operations.** Server CPU 96.0 µs vs
28.0 (prepared) / 59.5 (unprepared).

The decisive decomposition (`mongo_cpu_arms_nolog.json`): `get_children_miss` — the same shape
returning zero rows — costs **67.8 µs**, so the 11 rows cost 28.2 µs (2.57 µs/row) and **71% of the
operation is fixed per-command cost**. The two zero-row arms of different shapes agree within 0.65%
(`get_children_miss` 67.81 vs `get_node_miss` 67.37), so this fixed cost is shared, demonstrated
not assumed. The per-row side is closed: record fetch is 1.25 µs/row, no concentrated cost.

Why PostgreSQL wins: it plans once (psycopg3 `prepare_threshold=5`) and then pays ~28 µs; MongoDB
re-plans every call. Full analysis:
`/home/junyao/code/pageindex/ConDB/bench/db/report/ops/get_children.md`. Read it.

## Initial plan: make the classic engine cache hinted single-solution plans

Today these shapes **always miss and never populate** the classic plan cache.
`src/mongo/db/query/classic_plan_cache.cpp:141` — `shouldCacheQuery` returns false when a hint is
present and the query is not SBE-compatible. Measured on the real 10M dataset: 200 executions of
this shape across 64 distinct parents → classic cache **0 hits / 200 misses**
(`bench/db/report/evidence/review_20260807/v3_sbe.json`). Unhinted, the same query *does* populate
the cache — the hint is what disqualifies it. Tree workloads pin hints precisely for plan
stability, so the user who most wants a stable plan pays re-planning on every request.

Evidence of the prize, with its limits stated exactly. Under `trySbeEngine` — which does cache
these shapes (199 hits / 1 miss over the same 200) — server CPU falls **−13.9% to −16.0%** across
three retained runs (12/12 blocks in the best; 0 output mismatches over 1,971 elements; p99
−14.0%). **But that experiment switches engines, not just caching**: the winning plan is identical,
the execution engine is not, and the measured 14–16% exceeds the 11.35% that `QueryPlanner::plan`
occupies in this operation's profile (`perf_nolog/get_children_hit.sym.inclusive.txt`, 92,519
samples). **Caching alone is bounded near 11%.** Your first deliverable is the true number.

Design questions the change must answer:

- **What exactly does a hit skip?** One-solution planning is cheap-ish; `fillOutIndexEntries`
  measured ~0. Profile the cached-plan path on your own build before promising a number.
- **Cache key.** The hint must be part of the key — same filter, different hint must not collide.
- **Invalidation.** Index/collection drops must invalidate hinted entries exactly as unhinted.
- **Why is the exclusion there?** `git log -L` on `shouldCacheQuery`; state the original rationale
  in the PR. If it still holds, that is a finding.
- **Replanning.** A hinted entry can never be beaten by another plan; document the interaction with
  works-based replanning triggers.
- **Store-path cost.** What does the first (storing) execution now cost? A cache that saves 8 µs on
  hits and adds 30 on stores needs the hit rate stated.

## If the effect is insufficient, continue — in this order

The bar: no single-digit percentages. If caching alone lands under it, record the number and move
on.

1. **Extend a fast path to a bounded prefix scan.** This operation is an equality on a prefix of a
   compound index returning a small bounded set, with the remaining fields providing the sort. The
   express machinery cannot iterate today; extending it to "seek + bounded scan, skip plan selection
   and executor construction" attacks the same 71% fixed cost from the other side. No measurement
   exists for this — build the ceiling probe first (a deliberately-wrong version that skips the
   work) before writing the real thing. Coordinate with the `get_node` agent before touching
   express files; if they are mid-change, work behind a separate gate.
2. **The fixed command path itself** — dispatch, acquisition, transport, parse: the same ~68 µs
   floor `get_node` has. Profile on your own build; pick the largest attackable term.
3. If you find a better direction, take it — same discipline, same bar.

## Environment

- Fork `/home/junyao/code/mongo`; `origin` = `git@github.com:carsontung666/mongo.git`, `upstream` =
  mongodb/mongo. Pinned base `0561c098b99ac5e929005e70a2e37d7a97a82423`. Branch off the base.
- Build: `bazel build --config=opt //src/mongo/db:mongod` (~8 min, 96 cores). Tests: `resmoke`;
  plan-cache jstests exist — find and read them first.
- **Target is master.** Reference 7.0.34 source: `/home/junyao/code/mongo-r7.0.34`. The measured
  baseline (stock 7.0.34, `mongodb://localhost:57017`, db `bench`, `layout2_view`, 10M docs, no
  auth) cannot run your build — A/B on your own binary.
- Workload shapes: `/home/junyao/code/pageindex/ConDB/bench/db/bench_all_ops_layouts.py` — read it.
- A/B runner: `bench/db/condb_ab_campaign.py`.

## Discipline — five failure modes have recurred in this project

1. **Unit mixing.** Server CPU / client wall / retired instructions are three quantities. A wrong
   break-even was already produced here by dividing a client-wall cold-start cost by a server-CPU
   saving. `planningTimeMicros` is wall; `cpuNanos` is CPU.
2. **Unpaired arms.** Alternate within blocks; per-block paired deltas. An unpaired −14% became
   +0.5% paired.
3. **Inclusive/exclusive confusion.** The 11.35% planning figure above is *inclusive*; never add
   sibling inclusive percentages.
4. **Fabricated ceilings.** The ~11% bound is derived, not measured on your change — do not copy it
   into a results column.
5. **Non-like-for-like arms.** Verify output equality element-wise, every block.

Plus: **single binary**, env-var gate read once at startup (layout variance 2.6 pp on identical
source); **control endpoint** — an unhinted query is a natural one; **activation counter** (new-path
stores/hits) printed at exit. **Never benchmark while anything is compiling** — three sibling agents
also build here; announce dataset/duration/load first. Fresh connections differ 14–26% in P50 —
hold connections fixed across arms. Report observed spread; claim nothing smaller.

## Acceptance gate (per change you keep)

1. A test that **fails on the unpatched base** (e.g. hinted shape, N executions, assert hits > 0),
   artifact retained.
2. **Non-intrusion proof**, including `internalQueryExecYieldIterations: 1`; unhinted cache
   behaviour unchanged; different hint does not collide; index drop invalidates.
3. **Cold/store path priced**, hit rate stated.
4. **Blank-context agent review** to MongoDB query-team PR standards; fix, don't argue.
5. Honest limits, including the measured value of caching alone as distinct from the SBE numbers.

## Deliverables

- Branch pushed to `origin`, **Draft** PR. **No SERVER- ticket exists — do not invent one; never
  claim upstream-ready.** No AI-authorship traces in commits or PR text.
- Never push the ConDB repo; commit there locally only.
- A written record of every lead tried, with its number — negative results retained.
