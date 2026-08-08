# Task D — Let hinted single-solution classic queries use the plan cache

MongoDB is paying for this work; the deliverable is a change to `mongod`. This is the narrowest of
the four tasks and the one with the tightest measured bound — read that bound before starting,
because it caps what success looks like: **≤11–12% of short-read server CPU, and this project does
not ship single-digit results.** Your first job is to establish whether this clears the bar at all.

## What is wrong today

MongoDB re-plans these shapes on every call. `src/mongo/db/query/classic_plan_cache.cpp:141` —
`shouldCacheQuery` returns false when a hint is present and the query is not SBE-compatible, and
7.0.34 defaults to `forceClassicEngine` (`query_knobs.idl:881-888`). Measured directly on the real
10M-node dataset: 200 executions of the hinted `get_children` shape across 64 distinct parents give
the classic cache **0 hits / 200 misses**
(`bench/db/report/evidence/review_20260807/v3_sbe.json`); the hinted `get_node` shape records
+100 misses / 0 hits (`bench/db/runs/bottleneck_20260806/plan_cache_per_shape_raw.txt`).

Unhinted, the same `get_node` query carries a rejected plan and **does** populate the cache — so the
hint, not the shape, is what disqualifies it. Production tree workloads pin hints precisely for plan
stability, which makes this a JSON-tree-relevant gap: the user who most wants a stable plan is the
one who pays re-planning on every request.

The cost of re-planning, from an exclusive perf partition of `get_node`'s 71.7 µs of server CPU
(`bench/db/runs/bottleneck_20260806/decomp_get_node/get_node_phases.txt`): the
`QueryPlanner::plan` leaf bucket is **8.84 µs, 12.3%**; all of `getExecutorFind` is 15.46 µs, 21.6%.
For `get_children` (96.0 µs), `QueryPlanner::plan` is **11.35%** inclusive
(`perf_nolog/get_children_hit.sym.inclusive.txt`, 92,519 samples).

## The evidence, and exactly what it does and does not establish

Switching to `trySbeEngine` — under which these shapes **are** cached (SBE cache: 199 hits / 1 miss
over the same 200 executions) — moves server CPU:

| operation | runs | effect | notes |
|---|---|---|---|
| `get_children` | 3 retained | **−13.9% to −16.0%** | best-evidenced: 12/12 blocks in one run, 0 mismatches over 1,971 elements, p99 −14.0% |
| `get_node` | 3 retained | **−7.6% to −12.0%** | one run's block interval straddles zero [−50.6, +20.0] |
| `get_subtree` | 1 | −1.39% | within spread; 0 mismatches over 98,922 rows |
| `get_entity` | 1 | −0.05% | stays on `IDHACK` under both engines |

**This experiment measures switching engines, not caching.** The winning plan is identical under both
(`PROJECTION_SIMPLE`/`FETCH`/`IXSCAN`, same index, same keys and docs examined) but the execution
engine is not — under `trySbeEngine` the `winningPlan` carries a whole slot-based tree
(`plan_explainer_sbe.cpp:370`). On `get_children` the measured 14–16% **exceeds** the 11.35% planning
term, so at least part of the effect is the SBE runtime itself, not the cache. The
caching-attributable share is **bounded near 11–12%**, and the direct evidence that the arms differ
in more than cache state is the cold-start cost: after `planCacheClear`, SBE's first execution is
**+73.8 µs** (client wall, `get_children`) because it compiles a slot-based plan classic never
builds, and it is still +12.1 µs on execution 2, breaking even around 7–13 executions depending on
denominator.

So the honest framing of this task: **make the classic engine cache these plans, and measure what
that alone is worth.** If the answer is under 10% of server CPU on both short reads, the correct
deliverable is that number and a stop.

## Why this is not trivial — questions the design must answer

- **What is there to cache?** For a hinted single-solution query the planner produces one solution
  with no multi-planning. The saving is skipping `QueryPlanner::plan` and whatever of
  canonicalization/`fillOutIndexEntries` the cached-plan path avoids — `fillOutIndexEntries` itself
  measured ~0 (71.33/71.42/70.69 µs at 2/5/8 indexes), so do not expect savings there.
- **Cache key correctness.** The hint must be part of the key: the same filter with a different hint
  must not hit the same entry. Check how `planCacheKey` treats hints today for the shapes that *are*
  cached.
- **Invalidation.** Index drops/creates, collection drops, and catalog changes must invalidate
  hinted entries exactly as they do unhinted ones.
- **Why is the exclusion there?** `classic_plan_cache.cpp` has a reason for declining hinted
  queries — read the history (`git log -L` on that function) and state the original rationale in the
  PR. If the rationale still holds, that is the finding.
- **Replanning.** The classic cache has an eviction/replanning mechanism based on works; a hinted
  entry can never be beaten by a different plan, so decide and document how it interacts with
  replanning triggers.

## Environment

- Fork: `/home/junyao/code/mongo`. Remotes: `origin` = `git@github.com:carsontung666/mongo.git`,
  `upstream` = mongodb/mongo. Pinned base `0561c098b99ac5e929005e70a2e37d7a97a82423`. Branch off
  that base; do not build on another agent's branch.
- Build: `bazel build --config=opt //src/mongo/db:mongod`, ~8 min cold, 96 cores.
- Tests: `resmoke`. Plan-cache behaviour has existing jstests — find and read them first.
- 7.0.34 source for reference: `/home/junyao/code/mongo-r7.0.34`. **The change targets master**; the
  measured baseline is stock 7.0.34 at `mongodb://localhost:57017` (db `bench`, `layout2_view`,
  10M docs, no auth), which you cannot install builds on — your A/B runs use your own build.
- Workload: `/home/junyao/code/pageindex/ConDB/bench/db/bench_all_ops_layouts.py`. Read it.
- Prior evidence: `bench/db/report/ops/get_node.md` §5, `get_children.md` §4, and the artifacts they
  cite.
- Single-binary A/B campaign runner: `bench/db/condb_ab_campaign.py`.

## Measurement discipline — not optional

Five failure modes have recurred in this project. Check against all five.

1. **Unit mixing.** Server CPU, client wall, retired instructions are three quantities. The +73.8 µs
   cold-start figure above is client wall on `get_children`; do not divide it by a server-CPU saving
   — that exact mistake produced a wrong break-even here once already.
2. **Unpaired arms.** Alternate within blocks; report per-block paired deltas.
3. **Inclusive/exclusive confusion.** `QueryPlanner::plan` at 12.3% is an *exclusive leaf bucket* in
   `get_node_phases.txt` but *inclusive* in the `get_children` profile; never add sibling inclusive
   percentages.
4. **Fabricated ceilings.** The 11–12% bound above is derived, not measured on your change — do not
   copy it into a results column.
5. **Non-like-for-like arms.** Verify output equality element-wise, every block.

Plus: **single binary**, change gated on an environment variable read once at startup
(build-to-build layout variance is 2.6 pp on identical source); a **control endpoint** the gate
provably cannot fire on — an unhinted query is a natural one; an **activation counter** (cache
stores/hits by the new path) printed at exit and reported. **Never benchmark while anything is
compiling.** Announce dataset, duration and load first. Fresh connections differ 14–26% in P50 on
this box — hold connections fixed across arms. Report observed spread; claim nothing smaller.

## Acceptance gate

1. **A test that fails on the unpatched base** — e.g. hinted shape, N executions, assert cache hits
   > 0 — artifact retained.
2. **Proof of non-intrusion**, including `internalQueryExecYieldIterations: 1`, and specifically:
   unhinted queries' cache behaviour unchanged; a different hint on the same filter does not hit the
   first hint's entry; index drop invalidates.
3. **Cold-path cost measured**: what does the first (storing) execution now cost versus before? A
   cache that saves 8 µs on hits and adds 30 µs on stores needs the hit rate stated.
4. **A blank-context agent review** to MongoDB query-team PR standards. Fix what it finds; do not
   argue.
5. Honest limits, including the measured value of caching alone (as distinct from the SBE numbers
   above) on both short reads.

## Deliverables and constraints

- A branch pushed to `origin`, with a **Draft** PR.
- **There is no real SERVER- ticket. Do not invent one. Do not claim upstream-ready.**
- No AI-authorship traces in commits or PR text; this goes to MongoDB engineers.
- Never push the ConDB repo (`/home/junyao/code/pageindex/ConDB`) — commit there locally only.
- If you cannot do something, say so plainly with `file:line` evidence.

## What would make this not worth shipping

The most likely outcome, stated up front: caching alone measures single-digit on `get_node`
(bounded at 12.3%, and the cached-plan path still constructs an executor), the exclusion in
`classic_plan_cache.cpp` turns out to have a correctness rationale, or the store-path cost erodes
the hit-path saving at realistic shape diversity. Any of these, well measured, is a legitimate
deliverable — write it up and stop. Do not ship a single-digit patch.
