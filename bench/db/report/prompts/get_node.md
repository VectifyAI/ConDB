# Optimize MongoDB for `get_node` — single node lookup by natural key

You own this operation end to end. MongoDB is paying for this work; every deliverable is a change to
MongoDB's own code. **This document gives you an initial plan. Execute it first. If its measured
effect is insufficient, do not stop — continue down the fallback leads, and past them if necessary,
under the same discipline.**

Three sibling agents are working the other operations on this same box. Your lane is the **express /
fast-path eligibility code**. The plan cache belongs to the `get_children` agent, the driver to the
`get_entity` agent, cursor batching to the `get_subtree` agent — do not modify those areas, and
coordinate builds (see Discipline).

## The operation and the gap

Hinted `find_one` on `(tree_id, node_id)`, seven-field projection, `PROJECTION_SIMPLE → FETCH →
IXSCAN`. **MongoDB 0.196 ms vs PostgreSQL 0.092 ms P50, 2.14×.** Server CPU 71.7 µs vs 20.5
(prepared) / 46.5 (unprepared).

Where the 71.7 µs goes (exclusive partition, sums to 100% —
`/home/junyao/code/pageindex/ConDB/bench/db/runs/bottleneck_20260806/decomp_get_node/get_node_phases.txt`):
transport 27.6%, `getExecutorFind` 21.6% (of which `QueryPlanner::plan` 8.84 µs), dispatch 15.5%,
execution 8.4%, collection acquisition 7.8%, projection AST 5.9%, parse+teardown 5.8%. **Reading the
data is 1.99 µs, 2.8%.** The operation is fixed per-command cost, not data cost.

The full analysis: `/home/junyao/code/pageindex/ConDB/bench/db/report/ops/get_node.md`. Read it.

## Initial plan: give unique compound-index equality the fast path `_id` already gets

`allops_tree_node` is a **unique** index on `(tree_id, node_id)` — verified live — and the query
binds both fields with equalities, so it can match at most one document. It is semantically an `_id`
lookup. But `IDHACK` is `_id`-only, and on `upstream/master` `collectExpressEqualities()` in
`src/mongo/db/query/query_utils.h` requires a single `EQ`, so this shape falls through to full
planning on every call.

Measured value of the fast path on this exact query, real 10M collection, paired 14 blocks
(`bench/db/report/evidence/review_20260807/v6_idhack.json`): an unhinted `_id` equality costs
**−36.7% server CPU** (block min/max [−40.3, −29.9], 14/14) and −17.4% wall — **26.3 µs of 71.7**.
That was measured on `IDHACK`, not on a compound-key implementation; establishing the real number on
the real shape is the first half of your job.

**A starting point exists and is unverified.** Fork commit **`4fb23d8d1ba`** "Extend the express
fast path to compound equality predicates" (find it: `git branch --contains 4fb23d8d1ba`). It
generalises `collectExpressEqualities()` to conjunctions of equalities on distinct paths, adds
`orderEqualitiesForIndex()` (rejects an index unless its leading fields are exactly the constrained
paths; rejects multikey), and makes `LookupViaUserIndex` take a vector of operands. It has had **no
review, no measurement, no non-intrusion proof**. Do not trust its commit message — this project has
had commit messages with wrong numbers more than once. Re-derive everything.

Correctness constraints that must survive (both verified live on 7.0.34):

1. **A hint disqualifies the fast path** (`query_utils.cpp:52-59` requires an empty hint). Tree
   workloads pin hints for plan stability, so today the fast path is unavailable exactly where
   stability matters most.
2. **A sub-document `_id` matches by exact field order** — `{_id:{tree_id,node_id}}` returns one
   document, `{_id:{node_id,tree_id}}` returns none, both on `IDHACK`, silently. The compound-key
   path removes the need for that hazardous workaround; keep this in the PR's motivation.
3. A "unique" index that is **partial, sparse, or multikey** does not guarantee at most one match
   for an arbitrary equality, and collation must match. The eligibility rule must prove all of this
   locally.

## If the effect is insufficient, continue — in this order

The bar: this project does not ship single-digit percentages. If the initial plan lands below it,
say so with the number, then move on — the goal is the operation, not the patch.

1. **Relax the hint check when the hinted index is the one the fast path would pick.** Today any
   hint kills eligibility. If a hint naming exactly the chosen unique index could keep the fast
   path, the hinted production shape gets the win too. Nobody has established whether this is safe;
   it may be worth more than the initial plan.
2. **The fixed command path.** After a fast path, ~24 µs of server CPU remains above the per-command
   floor (`ping` = 21.48 µs server CPU): dispatch 11.1 µs, collection acquisition 5.6, projection
   AST 4.2, parse+teardown 4.1. No one has attacked any of these. Profile first, on your own build,
   and pick the largest attackable term.
3. **Transport, 19.8 µs (27.6%)** — the single largest component. Note 8.4% of `get_entity`'s
   profile samples sit in EDR/netfilter kernel modules on this box, so part of this is environment;
   separate that before claiming anything.
4. If you find a better direction than these, take it — with the same measurement discipline and the
   same bar.

## Environment

- Fork `/home/junyao/code/mongo`; `origin` = `git@github.com:carsontung666/mongo.git`, `upstream` =
  mongodb/mongo. Pinned base `0561c098b99ac5e929005e70a2e37d7a97a82423`. Branch off the base.
- Build: `bazel build --config=opt //src/mongo/db:mongod` (~8 min, 96 cores). Tests: `resmoke`;
  express coverage exists in `jstests/core/query/express.js` — read before adding.
- **Target is master.** Reference 7.0.34 source: `/home/junyao/code/mongo-r7.0.34`. The measured
  baseline (stock 7.0.34, `mongodb://localhost:57017`, db `bench`, `layout2_view`, 10M docs, no
  auth) cannot run your build — A/B on your own binary; treat the numbers above as context.
- Workload shapes: `/home/junyao/code/pageindex/ConDB/bench/db/bench_all_ops_layouts.py` — read it.
- A/B runner: `bench/db/condb_ab_campaign.py`.

## Discipline — five failure modes have recurred in this project

1. **Unit mixing.** Server CPU / client wall / retired instructions are three quantities.
   `planningTimeMicros` is wall; `cpuNanos` is CPU. Never compare across.
2. **Unpaired arms.** Alternate arms within blocks; report per-block paired deltas. An unpaired
   −14% here became +0.5% paired.
3. **Inclusive/exclusive confusion.** Never add sibling inclusive percentages.
4. **Fabricated ceilings.** Never copy a measured value into a ceiling column.
5. **Non-like-for-like arms.** Verify output equality element-wise, every block.

Plus: **single binary** — gate the change on an env var read once at startup (build-to-build layout
variance is 2.6 pp on identical source); a **control endpoint** the gate cannot fire on; an
**activation counter** printed at exit (fired/total). **Never benchmark while anything is
compiling** — three sibling agents also build on this box; announce dataset/duration/load before
heavy runs. Fresh connections differ 14–26% in P50 here — hold connections fixed across arms.
Report observed spread; claim nothing smaller than it.

## Acceptance gate (per change you keep)

1. A test that **fails on the unpatched base**, artifact retained.
2. **Non-intrusion proof**, including `internalQueryExecYieldIterations: 1`; eligibility must be
   locally decidable (parent-tells-child fine, global assumption not).
3. **The miss path priced** — the eligibility check runs on every `find`; measure what rejection
   costs, not only what acceptance saves.
4. **Blank-context agent review** to MongoDB query-team PR standards; fix, don't argue.
5. Honest limits: what you did not measure, what would falsify the result.

## Deliverables

- Branch pushed to `origin`, **Draft** PR. **No SERVER- ticket exists — do not invent one; never
  claim upstream-ready.** No AI-authorship traces in commits or PR text.
- Never push the ConDB repo (`/home/junyao/code/pageindex/ConDB`); commit there locally only.
- A written record of every lead you tried, with its number — negative results retained, not
  deleted.
