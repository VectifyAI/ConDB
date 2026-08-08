# `get_children` — record of leads, plan-cache lane

Running record for the plan-cache lane on `get_children`. Every lead is recorded with its number,
including the ones that did not pay. Negative results are retained deliberately: the point of this
file is that the next person does not repeat them.

Target is **master**, pinned base `0561c098b99ac5e929005e70a2e37d7a97a82423`, fork
`carsontung666/mongo`, branch `plan-cache-hinted-solutions`, worktree `/tmp/mongo-getchildren`.
The measured ConDB baseline (stock 7.0.34 on `:57017`) cannot run a master build, so every number
below is an A/B on one locally built binary, never a comparison against `:57017`.

Started 2026-08-09.

**Deliverables so far.** Branch `hinted-plan-cache-ceiling` pushed to `origin`; Draft PR
`carsontung666/mongo#4`, opened against the **fork's** master, not upstream. No SERVER ticket exists
and none was invented; the PR states in its first line that it is a measurement and not a proposal.

---

## L0 — Reading the exclusion on master, not on 7.0.34 · **premise correction, no measurement**

The task brief describes `shouldCacheQuery` as returning false "when a hint is present and the query
is not SBE-compatible". That is exactly right for 7.0.34 and **wrong for master**, and the
difference is not cosmetic.

`mongo-r7.0.34/src/mongo/db/query/classic_plan_cache.cpp:141`

```cpp
// ... In contrast, the SBE plan cache has the plan itself, so caching hinted queries could help
// to skip the plan construction.
if (!query.isSbeCompatible() && !findCommand.getHint().isEmpty()) {
    return false;
}
```

master `src/mongo/db/query/plan_cache/classic_plan_cache.cpp:143`

```cpp
if (!findCommand.getHint().isEmpty()) {
    return false;
}
```

The SBE carve-out is gone. On master, hinted queries are excluded from the classic plan cache
unconditionally, for both engines.

**This is what the SBE evidence in `get_children.md` §4 was actually measuring.** Under 7.0.34's
`trySbeEngine`, hinted queries were cache-eligible *because of that carve-out* — hence 199 hits / 1
miss against classic's 0 / 200. The −13.9%…−16.0% is the SBE plan cache doing its job on a shape the
classic cache refuses, not a generic "caching is worth 14%" result.

History, from `git log -L` on the function:

| commit | effect |
|---|---|
| `c12eaa0d7ef` SERVER-72803 "cache hinted sbe query" | **added** the SBE carve-out |
| `93ffbc04538` SERVER-124912 "Unhook SBE plan cache" | removed the SBE plan cache, and the carve-out with it |

**Original rationale, for the PR:** a classic cache entry does not hold a plan, only the data to
reconstruct one, so it is useful only for skipping multi-planning; hinted queries are generally not
multi-planned, so there is nothing to skip. The rationale is coherent and still holds *as stated*.
Any proposal to relax it has to argue that reconstruction from cached index tags is materially
cheaper than planning from scratch — which is the question L1 exists to answer.

## L1 — Does relaxing the exclusion do anything at all? · **no, on its own it is inert**

Checked before writing any code. The classic plan cache has exactly one writer:

- `plan_cache_util.cpp:135` `updateClassicPlanCacheFromClassicCandidates`
- reached only from `plan_cache_util.cpp:334` `ClassicPlanCacheWriter::operator()(const
  CanonicalQuery&, MultiPlanStage&, ...)`

The signature is the finding: **the only path into the classic cache runs off a `MultiPlanStage`.**
A hinted query yields one solution, is served by `SingleSolutionPassthroughPlanner`
(`get_executor.cpp:673`), and never reaches that writer.

So flipping the `shouldCacheQuery` hint test to `true` would produce **0 stores and 0 hits** — the
lookup would start succeeding at `buildCachedPlan`, find nothing, and fall through to full planning
exactly as before. The brief's framing ("make the classic engine cache hinted single-solution
plans") is therefore not a one-line change to an eligibility predicate; it requires **a
single-solution store path that the classic plan cache does not currently have.**

Three further obstacles sit behind that, all confirmed by reading, none yet measured:

1. **Key collision.** `canonical_query_encoder.cpp:1280` `encodeClassic` does not encode the hint.
   `encodeSBE` does (`:1341`). Two queries with identical filter, sort, projection and collation but
   different hints would share one classic cache key. Any real change must extend `encodeClassic`,
   which also changes the user-visible `planCacheShapeHash` for hinted queries — a discussion point
   for the PR, not a free change.
2. **Pinned entries need a ranking decision.** `plan_cache_debug_info.h:56` has
   `invariant(this->decision)`, and `explain.cpp:715` tasserts on it too. A single-solution entry has
   no `PlanRankingDecision`, so one must be synthesised or `DebugInfo` relaxed.
3. **No existing coverage.** No jstest under `jstests/core/query/plan_cache/` mentions hints at all.
   Whatever ships here needs its test written from scratch.

On the useful side, `PlanCache::setPinned` (`plan_cache.h:509`) is on the templated base and so is
already available to the classic cache. A pinned entry is created active and is immune to the
works-based active/inactive state machine — which is the right shape for a plan that can never be
beaten by an alternative, and answers the brief's "replanning" design question directly: there is no
works measurement to compare against, so works-based replanning does not apply.

## L2 — Ceiling probe for the win, before building the mechanism · **in progress**

Rather than build a store path, a key encoding, a synthetic decision and a test suite for a change
whose value is bounded near 11% by the brief's own derivation, the win is priced first. This is the
same discipline the brief prescribes for its fallback lead 1, applied to the primary lead.

`get_executor.cpp`, 87 lines, one file, gated by `MONGO_PROBE_HINTED_PLAN_MEMO` read once per
process:

- **store** — in `buildSingleSolutionPlan`, memoise the winning solution's `SolutionCacheData` under
  the plan cache key, for hinted queries only
- **hit** — in `buildCachedPlan`, after `shouldCacheQuery` declines, rebuild the solution from the
  memo via **`QueryPlanner::planFromCache`** — the same call a real cache hit makes — and run it
  through `SingleSolutionPassthroughPlanner`

Because the hit path is the real one, the number it produces bounds what a correct implementation
could deliver. What the probe skips is exactly what a real hit would skip: `PlanRanker::rankPlans`
and the `QueryPlanner::plan` beneath it.

**The probe is deliberately incorrect and is a measurement device only.** The memo is process-wide,
keyed on a hash that does not encode the hint (so two hints on one shape collide), is never
invalidated by index or collection drops, and never evicts. It also bypasses `shouldCacheQuery`,
which would trip the `dassert` at `query_planner.cpp:729` in a debug build; it is safe here only
because `kDebugBuild` is false under `--config=opt`.

Known non-intrusion detail: with the gate off, the only added work per query is storing a 32-bit
hash (`_probeKeyHash`) and testing one cached static bool. An earlier revision stashed the whole
`PlanCacheKey`; that would have cost an allocation per query in *both* arms. `PlanCacheKey` has a
deleted copy-assignment operator, which caught it at compile time — the hash is both free and
sufficient, since the memo is keyed on it anyway.

### Instrument

`bench/db/bench_children_plancache.py`. The gate is read at startup, so one process cannot serve
both arms; the harness runs **three mongods from the same binary over byte-identical dbpath copies**
— `baseline` (gate off), `probe` (gate on), `control` (gate off). `control − baseline` prices
process-to-process variance, and any probe effect smaller than it is noise.

Discipline held: server CPU is read from each arm's own connection thread
(`/proc/<pid>/task/<tid>/schedstat`) and never combined with client wall; arms rotate within blocks
and the reported effect is the median per-block paired delta; every block verifies element-wise
output equality across all three arms before timing; each arm holds one pinned connection
(`maxPoolSize=minPoolSize=1`) for the whole run. Block min/max is observed spread, not a confidence
interval.

Activation is checked, not assumed: the probe increments the classic hits counter, so
`serverStatus().metrics.query.planCache.classic` distinguishes "the probe fired" from "the classic
helper never ran". That last case is a live risk — master defaults `internalQueryFrameworkControl`
to `kTrySbeRestricted`, not `forceClassicEngine`, and the probe is only wired into
`ClassicPrepareExecutionHelper`.

**Result: −8.6% of server CPU. Under the bar. Lead closed.** Detail in L2d below.

### L2a — the first probe build measured nothing, because master plans somewhere else

The first probe was wired into `ClassicPrepareExecutionHelper::buildCachedPlan` and
`buildSingleSolutionPlan` in `get_executor.cpp`, which is where the brief points. It produced
**0 hits** on the probe arm over 176 executions, against 176 `skipped`.

The trap is that `skipped` looks like confirmation and is not: `incrementClassicSkippedCounter()` is
called from **three** different `buildCachedPlan` implementations. A non-zero skipped count proves
only that *some* helper declined the cache, not which one.

`explain` gave `explainVersion: 1` — classic engine, plan `PROJECTION_SIMPLE → FETCH → IXSCAN` — so
the engine was right. Raising `logComponentVerbosity.query` to 5 and reading the server's own
account settled it:

```
"Plan-based engine selection logic invoked."
"Classic chosen during plan-based engine selection"
"Planner: adding solution"          x3, and no "Query is not cachable"
```

Master runs this shape through **`get_executor_deferred_engine_choice_planning.cpp`**
(`preparePlanner`), not through `ClassicPrepareExecutionHelper`. That file has its own
`buildCachedPlan`, its own skipped counter and its own single-solution return.
`featureFlagGetExecutorDeferredEngineChoice` is on by default in this build — `encodeClassic` even
folds the flag into the plan cache key so entries cannot be shared across it.

Two things worth carrying forward from the same logs. The absence of "Query is not cachable" proves
`soln->cacheData` **is** populated for this hinted shape, so the store had data to work with. And
"Hint by name specified, restricting indices" confirms the hint is applied by narrowing the index
set before enumeration — which is what makes the shape single-solution in the first place.

The probe now sits in `preparePlanner`, replacing `QueryPlanner::plan()` for a hinted shape with a
memo lookup and returning through the identical `buildSingleSolutionPlanner` the planning path uses,
so the arms differ only in whether planning ran. `get_executor.cpp` was reverted to pristine.

**Cost of this detour: one build and one smoke run.** Recorded because the brief's file references
are 7.0.34-era and the next person will otherwise repeat it. The non-deferred
`ClassicPrepareExecutionHelper` path is deliberately left uninstrumented, so nothing here says
anything about a deployment that disables the flag.

### L2b — still zero hits: cost-based ranking is on by default, and owns its own single-solution return

The rebuilt probe, now in `preparePlanner`, *still* recorded 0 hits over 176 executions. The
reported deltas from that run (probe +19.95%, control +6.44%) are noise and are retained only as
evidence that the control arm does its job: an inactive probe should, and did, produce a spread
indistinguishable from two identical servers.

`preparePlanner` has **two** single-solution returns, and the one reached depends on a knob:

```cpp
if (plannerParams->isCBREnabled()) {
    return planWithCBR(...);          // its own SingleSolutionPassthroughPlanner return
}
auto solutions = uassertStatusOK(QueryPlanner::plan(*cq, *plannerParams));
if (1 == solutions.size() && ...) {
    return buildSingleSolutionPlanner(std::move(solutions[0]), cachedPlanHash);   // instrumented
}
```

`isCBREnabled()` is `planRanker != kMultiPlanner`, and the default in
`query_optimization_knobs.idl:1018` is **`QueryPlanRankerEnum::kMixed`**. So cost-based ranking is
on by default on master and the CBR branch is taken; the store I had added was on the branch that
never runs. The store is now also placed in `planWithCBR`'s single-solution return, which required
threading `planCacheKey` into that function.

**This is a second substantive master-vs-7.0.34 divergence.** The brief's model of the code — a
classic helper that plans, finds one solution, and hands it to a passthrough planner — is now spread
across a deferred-engine-choice file and a cost-based ranker, each with its own cache consultation
and its own single-solution exit. Any real implementation of this change has to cover both exits,
not one.

To stop paying a build per hypothesis, the probe now logs why it declined to store
(`hinted` / `eligibleForPlanCache` / `hasCacheData`) and logs the key hash when it does store, at
debug level 1 under the query component. Diagnosing the next miss is a log read, not a rebuild.

**Cost of this detour: one further build and one smoke run.**

### L2c — activation confirmed

With the store placed on the CBR branch, the probe fires: **175 hits / 176 executions** on the probe
arm, **0** on baseline and **0** on control. The single non-hit is the first execution, which stores.
Baseline and control still record 176 `skipped` each, unchanged from stock.

That is the activation proof the earlier runs lacked, and it is what licenses reading any number
from this probe at all.

**What the probe skips on master is not what the brief's 11.35% describes.** The brief derives its
"caching alone is bounded near 11%" from a 7.0.34 profile in which `QueryPlanner::plan` is 11.35%
inclusive. On master the probe returns before `planWithCBR`, so it skips **both** `QueryPlanner::plan`
*and* the cost-based ranker's cardinality estimation — work that did not exist in 7.0.34. The
measured ceiling here should therefore be expected to exceed 11.35%, and **must not be reported as
confirming or refuting that figure**: they are bounds on different amounts of work, on different
code, measured on different instruments. The 11.35% is not carried into any results column.

### Dataset

A prefix of the real `bench.layout2_view` tree copied from `:57017` into a local dbpath, with the
same four indexes. The 64-parent cohort is chosen from the copy and then **verified against the real
server** — any parent whose fan-out differs, because the copy limit truncated its children, is
dropped rather than corrected. Source confirmed at 10M documents with a cohort at fan-out 6–14,
median 10, matching `get_children.md`.

Not reproduced: total collection size, which changes B-tree descent depth by a couple of levels.
This follows the precedent set by `bench_bottleneck_local_mongod.py`, and is stated rather than
hidden.

---

### L2d — the ceiling for plan caching on this operation is −8.6% of server CPU · **under the bar**

Three campaigns, 20 blocks x 40 sweeps x 64 parents each, on a 1.5M-document clone. The gate was
rotated across all three ports and dbpath copies, so a server-specific bias would show up as the
effect failing to follow the gate.

Artifacts: `runs/getchildren_plancache_20260809/probe_ceiling{,_rot1,_rot2}.json`, summarised by
`bench/db/analyze_children_plancache.py` into `summary.json`.

| rotation | probe ran on | probe vs baseline | control vs baseline | probe vs control |
|---|---|---|---|---|
| 0 | 57022 | −7.37% | +1.82% | −10.32% |
| 1 | 57023 | −8.63% | −0.79% | −8.23% |
| 2 | 57021 | −9.00% | +1.72% | −10.41% |
| **median across rotations** | | **−8.63%** | **+1.72%** | −10.32% |

Server CPU, per-block paired, median over blocks. Absolute medians in rotation 2: baseline 99.45,
probe 90.93, control 101.22 µs/op.

**The effect follows the gate, not the machine.** The probe produced −7.4%, −8.6% and −9.0% while
running on three different ports against three different dbpath copies. Rotation 1 was 20/20 blocks
better; rotation 2, 17/20.

**Activation, every campaign:** probe **53,055 hits / 53,056 executions**; baseline and control **0
hits**, 53,056 `skipped`. **Correctness, every campaign:** 1,320 elements compared element-wise
across all three arms, every block, 20 blocks, zero mismatches.

**The control is not zero, and that is the floor.** Two servers from the same binary on
byte-identical data with the gate off in both differ by −0.79% to +1.82%. Nothing smaller than about
two percentage points is demonstrated here, which is why the effect is quoted as ≈−8.6% and not to a
tighter figure.

**Client wall, kept separate and never combined with CPU:** −1.84%, −4.37%, −5.35% across the three
rotations. Smaller than the server-CPU effect, as expected for an operation the source document puts
at roughly 59% server / 41% client — a ~8 µs server saving sits inside a ~200 µs round trip.

**Contaminated blocks, reported rather than removed.** Rotation 0 had four blocks (4, 6, 7, 8) where
the probe read +30% to +59% while the control in those same blocks read +0.1% to +1.4%; rotation 2
had two such blocks. Because the control was clean in them, this is not general box noise but
unexplained arm-specific contamination, and it remains unexplained. Excluding them moves rotation 0
to −8.10% and rotation 2 to −9.54% — *closer to the other rotations, not further*, so the exclusion
would strengthen the result and is therefore not taken. The headline numbers include every block.

### Why this closes the lead

The bar was: no single-digit percentages. **−8.6% is single digit**, and it is an upper bound in a
strong sense — a correct implementation is strictly more expensive than this probe on both paths:

- the **hit** path would consult the real `PlanCache` (partitioned lock, entry state check, eviction
  bookkeeping) instead of one `unordered_map` lookup under a plain mutex;
- the **store** path would build a synthetic `PlanRankingDecision` and `DebugInfo` and call
  `setPinned`, instead of moving a cloned pointer into a map;
- **every query on the server**, hinted or not, would pay a slightly larger `encodeClassic` once the
  hint is folded into the key.

None of those costs are in the −8.6%. The real change lands below it, on an operation where the
brief's own decomposition puts 71% of the cost in fixed per-command work — of which planning, now
measured, is about 8.6 points.

**Not built:** the single-solution store path, the hint in `encodeClassic`, the synthetic ranking
decision, invalidation and collision tests. Priced first, deliberately, and not worth building at
this number. The probe patch is retained on the branch as the evidence, gated off by default and
labelled as a measurement device.

**What would change this verdict:** a workload where planning is a larger share — many distinct
hinted shapes on a wider index set, or a server where CBR does more estimation work. Nothing here
bounds those. This number is for *this* shape on *this* build.

## L3 — where the fixed per-command cost actually goes · **profiled; no concentrated term to attack**

**L3 was not skipped because L2 failed — it was run to decide what to do next.** 30 s at 999 Hz,
22 K samples, restricted to the single mongod connection thread serving the workload, 176,303
operations driven during the capture. Artifacts:
`runs/getchildren_plancache_20260809/perf/children.{inclusive,exclusive}.txt`, produced by
`bench/db/profile_children_fixedcost.py`. perf resolves every user frame here because this mongod is
this account's own build running as this account's uid.

### The call structure (inclusive — sibling percentages must NOT be added)

| frame | inclusive |
|---|---|
| `SessionWorkflow::Impl::_doOneIteration` | 93.73% |
| ├ `_dispatchWork` | 77.48% |
| ├─ `ServiceEntryPointShardRole::handleRequest` | 74.76% |
| ├── `executeCommand` | 71.18% |
| ├─── `FindCmd::Invocation::run` | **64.69%** |
| ├──── `getExecutorFind` | **24.60%** |
| ├───── `planRanking` | 20.83% |
| ├────── `preparePlanner` | 15.09% |
| └──── `PlanExecutorImpl::getNextBatch` | **14.87%** |

Two gaps are the story. From `_doOneIteration` (93.73) down to `FindCmd::run` (64.69) is **≈29
points of session, dispatch and command-framework overhead outside the command itself**. And inside
`FindCmd::run`, planning (24.60) and execution (14.87) together leave **≈25 points** for command
parsing, collection acquisition, reply building and cursor handling.

This also explains the L2 number honestly: `getExecutorFind` is 24.60% inclusive, but a cache hit
does not remove all of it — it still constructs an executor, and `planFromCache` still runs. −8.6%
out of a 24.60% envelope is the expected shape, not a disappointment.

### Where the cycles pool (exclusive, aggregated over the whole profile, 99.0% accounted)

| category | exclusive |
|---|---|
| mongod C++, everything else | 54.01% |
| kernel — syscall, net, sched | 15.24% |
| allocator — tcmalloc, `new`/`delete` | **8.53%** |
| BSON build/parse | 7.88% |
| WiredTiger storage | 7.62% |
| libc mem/str primitives | 2.77% |
| third-party kernel hooks (`tmhook`/`bmhook`) | 1.22% |

**The single most important number in this table is not in it: the largest individual leaf function
is 2.10%**, and it is a tcmalloc allocation path. The next seven are 1.49, 1.28, 1.24, 1.22, 1.15,
1.09, 1.07 — four of which are also allocator or `memmove`. **There is no hotspot.** The fixed cost
is genuinely diffuse: half of it is mongod's own C++ spread across thousands of functions none of
which reaches 1%.

Consequences for the brief's fallback 2 ("pick the largest attackable term"): **there isn't one.**
The largest attributable *mechanism* is allocator traffic at 8.53% — itself single-digit, and
reachable only by a broad allocation-reduction campaign across the command path, not a targeted
change. The 15.24% of kernel time is the network round trip and is not addressable without changing
the protocol. WiredTiger at 7.62% confirms the source document's finding that the per-row side is
closed.

**Environmental caveat, stated because it distorts absolute attribution:** this box runs
syscall-hooking kernel modules (`tmhook`, `bmhook`, `klp_x64_sys_call`). They appear at 1.22%
exclusive but wrap syscalls, so the 15.24% kernel figure is inflated relative to a clean machine.
This affects all arms equally and so does not touch any A/B delta in L2, but no absolute figure here
should be transferred to a different host.

## Still to run

- **L4** — extend a fast path to a bounded prefix scan (brief's fallback 1). This is now the
  strongest remaining lead precisely *because* L3 found no hotspot: it does not attack a hot
  function, it skips whole layers. The envelope is `getExecutorFind` at 24.60% inclusive plus part
  of the ≈25 points of parse/acquire/reply inside `FindCmd::run`; L2 has already shown ≈8.6 of that
  envelope is reachable by caching alone, so the headroom for skipping construction as well is the
  remainder — **bounded, not measured**. Ceiling probe first. Coordinate with the `get_node` agent
  before touching express files; they are mid-change on `express-compound-equality`, so this must
  work behind a separate gate.
- **L5** — the real M1 mechanism. **Not planned.** L2 priced it under the bar; it is recorded as
  closed, not deferred.
