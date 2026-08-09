# `get_children` — record of leads, plan-cache lane

Running record for the plan-cache lane on `get_children`. Every lead is recorded with its number,
including the ones that did not pay. Negative results are retained deliberately: the point of this
file is that the next person does not repeat them.

Target is **master**, pinned base `0561c098b99ac5e929005e70a2e37d7a97a82423`, fork
`carsontung666/mongo`, branches `hinted-plan-cache-ceiling` and `express-prefix-scan`, worktree
`/tmp/mongo-getchildren`.
The measured ConDB baseline (stock 7.0.34 on `:57017`) cannot run a master build, so every number
below is an A/B on one locally built binary, never a comparison against `:57017`.

Started 2026-08-09.

**Deliverables.** Two branches pushed to `origin`, both Draft PRs against the **fork's** master,
not upstream. No SERVER ticket exists and none was invented.

| PR | branch | what | outcome |
|---|---|---|---|
| [#4](https://github.com/carsontung666/mongo/pull/4) | `hinted-plan-cache-ceiling` | measurement probe, not a proposal | ceiling ≈−10.3%, **lead closed** |
| [#6](https://github.com/carsontung666/mongo/pull/6) | `express-prefix-scan` | extends express to a bounded index prefix scan | **−20.8% instructions, −35.5% server CPU, −18.5% wall** |

The second is the result. The first is why it was the thing worth building.

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

Checked before writing any code. The classic plan cache has one writer function,
`plan_cache_util.cpp:135` `updateClassicPlanCacheFromClassicCandidates`, with **two** callers:

- `plan_cache_util.cpp:340`, `ClassicPlanCacheWriter::operator()(const CanonicalQuery&,
  MultiPlanStage&, ...)`
- `classic_runtime_planner_for_sbe/multi_planner.cpp:135`, a direct call bound as a
  `MultiPlanStage::OnPickBestPlan` callback at `multi_planner.cpp:32`

*(Corrected after review — an earlier version of this file, and of the commit message on the branch,
said "exactly one writer, reached only from `ClassicPlanCacheWriter`". That was wrong. The
conclusion is unchanged, because both callers are `MultiPlanStage` pick-best-plan callbacks, but
the claim as written was false.)*

The signature is the finding: **every path into the classic cache runs off a `MultiPlanStage`.**
A hinted query yields one solution, is served by `SingleSolutionPassthroughPlanner`
(`get_executor.cpp:673`), and never reaches either caller.

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

## L2 — Ceiling probe for the win, before building the mechanism · **closed, ≈−10.3%**

Rather than build a store path, a key encoding, a synthetic decision and a test suite for a change
whose value is bounded near 11% by the brief's own derivation, the win is priced first. This is the
same discipline the brief prescribes for its fallback lead 1, applied to the primary lead.

One file, gated by `MONGO_PROBE_HINTED_PLAN_MEMO` read once per process:

- **store** — memoise the winning solution's `SolutionCacheData` under the plan cache key, for
  eligible hinted queries only
- **hit** — before planning runs, rebuild the solution from the memo via
  **`QueryPlanner::planFromCache`** — the same call a real cache hit makes — and return it through
  the same `SingleSolutionPassthroughPlanner` the planning path returns

*(The first version of this was written into `get_executor.cpp`, which turned out not to be the file
master executes — see L2a — and the store was placed on the non-CBR branch, which turned out not to
be the branch master takes — see L2b. It now lives in
`get_executor_deferred_engine_choice_planning.cpp`, in `preparePlanner` and `planWithCBR`.)*

Because the hit path is the real one, the number it produces bounds what a correct implementation
could deliver. What the probe skips is exactly what a real hit would skip: `PlanRanker::rankPlans`
and the `QueryPlanner::plan` beneath it.

**The probe is deliberately incorrect and is a measurement device only.** The memo is process-wide,
is never invalidated by index or collection drops, and never evicts. It also bypasses
`shouldCacheQuery`, which would trip the `dassert` at `query_planner.cpp:729` in a debug build; it is
safe here only because `kDebugBuild` is false under `--config=opt`. Review found two further
wrong-results defects in the first version of it — see L2e — both since fixed.

Known non-intrusion detail: with the gate off, the only added work per query is testing one cached
static bool, at most three times, before any other work. An earlier revision stashed the whole
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

**Result: ≈−10.3% of server CPU as an upper bound; a correct build lands below it. Lead closed.**
Detail in L2d below.

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
so the two arms run the same plan. (An earlier version of this line claimed they "differ only in
whether planning ran"; review showed that was overstated — see L2e.) `get_executor.cpp` was
reverted to pristine.

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

### L2d — the ceiling for plan caching on this operation is ≈−10.3% of server CPU · **at the bar, and an upper bound**

Three campaigns, 20 blocks x 40 sweeps x 64 parents each, on a 1.5M-document clone, **on the
review-corrected probe** (L2e) and on a box verified quiet for 60 consecutive seconds first. The
gate was rotated across all three ports and dbpath copies, so a server-specific bias would show up
as the effect failing to follow the gate.

Artifacts: `runs/getchildren_plancache_20260809/v2_rot{0,1,2}.json`, summarised by
`bench/db/analyze_children_plancache.py` into `summary_v2.json`.

| rotation | probe ran on | probe vs baseline | control vs baseline | probe vs control | blocks better |
|---|---|---|---|---|---|
| 0 | 57022 | −10.29% | −0.59% | −9.55% | 15/20 |
| 1 | 57023 | −11.58% | +0.35% | −10.72% | **20/20** |
| 2 | 57021 | −10.33% | −1.28% | −9.36% | **20/20** |
| **median across rotations** | | **−10.33%** | **−0.59%** | −9.55% | |

Server CPU, per-block paired, median over blocks. Absolute medians: baseline 91.6–94.6, probe
81.5–84.5, control 91.8–93.6 µs/op.

**The effect follows the gate, not the machine**, on three different ports against three different
dbpath copies. Two of the three rotations improved in every single block.

**Activation, every campaign:** probe **53,055 hits / 53,056 executions**; baseline and control **0
hits**, 53,056 `skipped`. **Correctness, every campaign:** 1,320 elements compared element-wise
across all three arms, every block, zero mismatches.

**The control floor is now tight**: −0.59%, +0.35%, −1.28%, against ±1.8% in the earlier campaigns.
That is the whole reason these numbers supersede the first set.

**Client wall, kept separate and never combined with CPU:** −5.08%, −6.92%, −5.68%.

### Superseded: the first campaigns read −8.6%

The first three campaigns (`probe_ceiling{,_rot1,_rot2}.json`) gave −7.37% / −8.63% / −9.00%,
median −8.63%. They are retained but **superseded**, for one reason: they ran while the box was
still settling from concurrent sibling builds, and their control arm drifted +1.82% / −0.79% /
+1.72% where the corrected runs hold −0.59% / +0.35% / −1.28%.

**Two things changed between the two sets and they cannot be cleanly separated**: the probe was
corrected after review, and the box was quieter. What can be said is that the corrections *add* work
to the hit path — an eligibility check and a full key-string comparison — so they cannot explain an
effect getting larger. The difference is attributable to measurement conditions, not to the change.
Reported this way rather than quietly replacing one number with another.

### Why this closes the lead

The bar was: no single-digit percentages. The ceiling measures **≈−10.3%**, which nominally clears
it — but only as a ceiling, and by 0.3 of a point, which is **smaller than the ±1.3-point control
floor**. It is an upper bound in a strong sense: a correct implementation is strictly more expensive
than this probe on every path —

- the **hit** path would consult the real `PlanCache` (partitioned lock, entry state check, eviction
  bookkeeping) instead of one `unordered_map` lookup under a plain mutex;
- the **store** path would build a synthetic `PlanRankingDecision` and `DebugInfo` and call
  `setPinned`, instead of moving a cloned pointer into a map;
- **every query on the server**, hinted or not, would pay a slightly larger `encodeClassic` once the
  hint is folded into the key.

None of those costs are in the −10.3%. **The honest reading is therefore that a correct
implementation is not demonstrated to clear a ten-percent bar**: it lands below 10.3% by an
unmeasured amount, and the margin to spend is thinner than the run-to-run floor. Closing the lead is
a judgement that the remaining machinery — a single-solution store path, hint-and-more in
`encodeClassic`, a synthetic ranking decision, invalidation and collision tests — is not worth
building to find out, on an operation where a fast path has a ≈24 µs envelope against this ≈9.5 µs
one (L4a).

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
does not remove all of it — it still constructs an executor, and `planFromCache` still runs. −10.3%
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

## L4a — what a fast path is worth, measured · **24 µs per command; the first result that clears the bar**

Before building a bounded-prefix-scan fast path, price what bypassing plan selection and executor
construction is worth at all. There is an exact lever for this that needs no rebuild and no knob:
`isIdHackEligibleQueryWithoutCollator` (`query_utils.h:41`) requires `findCommand.getHint().isEmpty()`,
so on one server, over the same `_id` index, returning the same document:

    express   find({_id: X})                  -> express fast path
    planned   find({_id: X}, hint={_id: 1})   -> CanonicalQuery, planning, executor construction

Both arms run in **one process** on their own pinned connections, so there is no process-to-process
bias to correct. `control` is a second express connection and prices connection-to-connection
variance. `bench/db/bench_express_ceiling.py`, artifact
`runs/getchildren_plancache_20260809/express_ceiling_clean.json`, 20 blocks x 40 sweeps x 64 ids.

| arm | server CPU µs/op | vs express, median paired | spread over blocks |
|---|---|---|---|
| express | ≈30.3 | — | — |
| planned | ≈54.2 | **+79.23%** | [+66.80, +92.21] |
| control | ≈30.2 | −0.44% | [−6.41, +7.34] |

**Express saves 44.05% of the planned arm's server CPU, about 24 µs per command.** The effect is an
order of magnitude outside the control floor, and every block agrees.

**A first attempt at this run was discarded, not reported.** A sibling agent started a bazel build
partway through it; the control spread blew out to [−16.53, +22.86] and the absolutes read 150–300 µs
for a point query. It was re-run only after the box was verified quiet for 60 consecutive seconds,
and confirmed clean at the end. The contaminated numbers are not in any table here.

### What this does and does not say about `get_children`

**It is a different shape and does not transfer as a `get_children` result.** What it establishes is
the size of the machinery a fast path removes: on this build, plan selection plus executor
construction plus the surrounding `CanonicalQuery` work costs **≈24 µs per command** on a minimal
query.

The source document's decomposition licenses treating that as *fixed* per-command cost — its two
zero-row arms of different shapes agreed within 0.65% — so the absolute transfers better than the
percentage. Against `get_children`'s ≈100 µs server CPU on this same build and instrument, an
envelope of ≈24 µs is **≈24%**, of which L2 has already shown ≈8.6 points are reachable by caching
alone. The incremental headroom for skipping construction as well is therefore ≈15 points.

**Both figures are bounds derived from a different shape, not measurements on `get_children`, and
must not be reported as results for it.** A bounded-scan fast path must additionally iterate,
produce ten rows rather than one, and satisfy the sort, so it would recover **less** than 24 µs —
never more.

That said, this is the first number in this lane with room above the bar, and it is why L4 is worth
building where M1 was not.

## L2e — blank-context review of the branch, and what it changed

A reviewer with no context on this work was given the branch and asked to apply MongoDB query-team
standards adversarially. It found one wrong-results defect, one false claim of mine, and a lint
violation. All were fixed rather than argued; the findings are recorded here because the negative
ones are the point of this file.

**1. Wrong results — the probe kept only one of `shouldCacheQuery`'s five exclusions.**
`shouldCacheQuery` rejects hint, `min`, `max`, explain-cache-ineligible and tailable. That list is
not arbitrary: `encodeClassic` encodes **none** of them, so those queries are excluded precisely
because the key cannot tell them apart from a query without them. The probe tested only the hint.

The concrete failure: `find({a: {$gte: 0}}).hint("a_1")` stores an entry; the same shape with
`.min({a: 5}).max({a: 10})` hashes to the same key, hits that entry, and is served a plan built
**without the min/max bounds** — returning every document with `a >= 0`. Silently, no error. It is
one-directional and self-reinforcing: planning a min/max query never sets `soln->cacheData`, so such
a query can only ever *read* another query's unbounded entry and never store its own correct one.
Every min/max query carries a hint (the planner rejects min/max without one), so all of them are
read candidates.

**This does not invalidate the result.** The measured workload is a single hinted shape with no
min/max, no tailable and no explain — the reviewer's own words: "the verification covered the
configuration that happens to be safe." But it means the 53,056-execution equality check proved less
than it appeared to, and the probe's own comment understated its danger. Fixed by
`hintedPlanMemoEligible()`, which now mirrors every `shouldCacheQuery` exclusion except the hint.

**2. Wrong results — keying on the 32-bit hash alone.** The real cache hashes to a bucket then
compares the full `PlanCacheKey`; the probe compared nothing. Since `tagAccordingToCache` validates
only tree *topology*, not field paths, a collision between two structurally isomorphic shapes over
different fields would tag the wrong predicate and build bounds for the wrong field. Fixed: the memo
now stores the key string and compares it on hit.

**3. My "exactly one writer" claim was false.** Corrected in L1 above.

**4. The measurement claim was overstated.** The branch said the arms "differ only in whether
`QueryPlanner::plan()` ran". They do not. With `planRanker` at its default `kMixed` the baseline
runs `planWithCBR`, and the probe returns before it, so what is actually skipped is
`rankPlans()` + `QueryPlanner::plan()` + `SolutionCacheData` construction + a second `WorkingSet`
allocation + `incrementPlannerInvocationCount()`; the probe also bypasses the subplanning check and
sits outside the `if (!replanning)` guard, and returns a solution with no `cacheData` where the
planning path always sets one. For a non-explain single-solution hinted find the two arms do return
an equivalent planner — the reviewer agreed the result is "directionally sound" — but the sentence
was wrong and is now restated. **The servers ran the default `internalQueryPlanRanker`, `kMixed`;
the number would mean something different under `multiPlanner`.**

**5. A fourth reason the ceiling is optimistic**, on top of the three already listed: the probe's
lookup is a hash into a flat map, where the real one also walks LRU/budget bookkeeping and the
`getCacheEntryIfActive` works-state check.

**6. `std::unordered_map` is a banned name** (`MongoBannedNamesCheck.cpp:108`) and would have failed
clang-tidy. Now `stdx::unordered_map`. Note the converse, which I had backwards: `std::mutex` is
**correct** here — `mongo/stdx/mutex.h` was deleted by `685559c5fe`, which is why my first build
failed to find it. That was not a missing bazel dependency, and the earlier note in this file
saying the target lacked the dep was wrong.

Also fixed: the decline-to-store log fired for every *non-hinted* query when the gate was on, adding
asymmetric work to the probe arm; and two dead assignments (`indexFilterApplied`, `solutionHash`)
that `planFromCache` never reads and which made the memo look more faithful than it was.

**The probe was rebuilt with these fixes and every campaign re-run**, because the eligibility check
and the key comparison both add work to the hit path. Numbers in L2d are from the corrected build.

## L4b — express already does a bounded range seek; it just refuses to keep going · **read-only finding**

The brief describes the express machinery as one that "cannot iterate", and I assumed extending it
would be major surgery on the plan. Reading it (no files touched — this lane belongs to the
`get_node` agent) says otherwise.

`src/mongo/db/exec/express/express_plan.h`, `LookupViaUserIndex::consumeOne` at `:666`:

```cpp
// Build the start and end bounds for the equality by appending a fully-open bound for each
// remaining field in the compound index.
BSONObjBuilder startBob, endBob;
... for (int i = 1; i < desc->getNumFields(); ++i) { startBob.appendMinKey(""); endBob.appendMaxKey(""); }
auto indexCursor = sortedAccessMethod->newCursor(opCtx, ru, true /* forward */);
indexCursor->setEndPosition(endKey, true /* endKeyInclusive */);
...
if (isSuccessfulResult(progress)) {
    _exhausted = true;      // <- stops after exactly one document
    return Exhausted{};
}
```

**It is already a bounded prefix scan.** It seeks to the first key of an equality range with
MinKey/MaxKey padding over the remaining compound fields, and it already sets an end position on the
cursor. Everything needed to walk `(tree_id, parent_id)` = constant and stop at the end of the run
is present. Two things stop it:

1. `_exhausted = true` after the first successfully-consumed document (`:736`), and
2. the cursor is a local built fresh inside `consumeOne` (`:694`), so there is nothing to resume from
   on a subsequent call.

So the shape of the change is: hoist the cursor to a member, replace the unconditional `_exhausted`
with `nextKeyValueView()` until the end position is passed, and relax the eligibility predicate that
currently requires the equality to identify at most one document. `ExpressPlan` is already templated
on `IteratorChoice` (`:1146-1150`) with three implementations, so a fourth is a supported extension
point rather than a rewrite.

The sort is free for this operation: `get_children` sorts on `(path, node_id)`, which are exactly the
trailing fields of `allops_tree_parent_path` that the MinKey/MaxKey padding walks in order, so a
forward cursor emits rows already sorted. That is the same reason the current plan has no `SORT`
stage.

**Not attempted here.** These are the `get_node` agent's files and they are mid-change on
`express-compound-equality`. The finding has been relayed to them along with the L4a numbers. What
this changes is the estimate: extending express to this shape is a tractable change to one iterator,
not the rewrite the brief's "cannot iterate" phrasing implies, and L4a says the envelope is ≈24 µs
of a ≈92 µs operation.

## L4c — concrete plan for the express extension, and the hazard in it · **handed off, not attempted**

Completing L4b by reading the driver as well as the iterator. `PlanExecutorExpress::getNext`
(`plan_executor_express.cpp:368`) already does the right thing:

```cpp
while (!haveOutput) {
    if (_plan.exhausted()) { return ExecState::IS_EOF; }
    _opCtx->checkForInterrupt();
    progress = _plan.proceed(_opCtx, [&](RecordId rid, BSONObj obj) { ...; haveOutput = true; ... });
```

It loops, checks `exhausted()` at the top, and returns one document per call. **The express framework
already supports returning many documents.** Nothing above the iterator needs to change. Combined
with L4b — the range seek and end position already exist — the entire restriction to one document
lives in two lines of `LookupViaUserIndex::consumeOne`.

### The change

1. Hoist the cursor. `:694` builds `indexCursor` as a local inside `consumeOne`; it becomes a member,
   built on first call and advanced with `nextKeyValueView()` afterwards.
2. Replace `_exhausted = true` at `:736` with an advance, setting `_exhausted` only when the cursor
   passes the end position already set at `:695`.
3. Relax the eligibility predicate that requires the equality to identify at most one document
   (`isEqualityExpressEligibleQuery`). **This is the file the `get_node` agent is changing**, which
   is why nothing here was attempted.
4. `get_children` needs no sort work: `(path, node_id)` are the trailing fields of
   `allops_tree_parent_path` that the MinKey/MaxKey padding already walks in order.

### The hazard, which is the real work

`releaseResources()`/`restoreResources()` (`:747`, `:751`) currently only null and re-fetch
`_indexCatalogEntry`, because a cursor that never outlives one call needs nothing else. A persisted
cursor must be saved and restored across yields — `save()`, `restore()`,
`detachFromOperationContext()`, `reattachToOperationContext()` — and a bounded scan yields *between*
documents where a point lookup never did.

Getting this wrong does not fail loudly; it returns wrong or duplicated rows under concurrent
writes. **Whoever builds this must test under `internalQueryExecYieldIterations: 1`**, which the
brief already requires, and should treat that as the acceptance gate rather than a formality. It is
the reason this is a careful change rather than a small one, and the reason it was not started
speculatively at the end of a long session in another agent's lane.

### Why it is still the right lead

L4a measured the envelope at **≈24 µs per command** against this operation's ≈92 µs — roughly 26% —
where the plan-cache ceiling measured ≈9.5 µs. L3 found no hotspot to attack instead: the largest
leaf in the whole profile is 2.10%. A fast path does not need a hotspot, because it skips layers
rather than optimising one.

## L6 — is a hinted baseline the right reference? · **yes: real plan caching is worth +0.43%, i.e. nothing**

The `get_node` agent raised the sharpest challenge of this lane: a hinted control arm re-plans on
every call, because a hint disqualifies plan caching. Measuring a fast path against it therefore
measures *fast path vs uncached planning*, which would overstate production gain for a workload
whose queries would otherwise hit the cache. They had been burned by exactly this — a 28–34% figure
from a hinted campaign, three builds, three nulls.

Tested directly, in **retired instructions** per their instrument advice (wall and CPU cannot resolve
this; instructions hold to ~0.05% here). One server, one process, three pinned connections,
`bench/db/bench_children_hint_vs_cached.py`, artifact `runs/.../hint_vs_cached.json`:

| arm | insn/op | vs hinted | spread over 9 blocks |
|---|---|---|---|
| hinted (uncached, replans every call) | 435,810 | — | — |
| **unhinted (cache hit)** | **437,809** | **+0.43%** | [+0.33, +0.59] |
| control (second hinted connection) | 435,720 | −0.05% | [−0.16, +0.01] |

Plan cache counters during warm-up confirm the arms did what they claim: **5,590 hits** (unhinted)
against **10,764 skipped** (the two hinted arms).

**The plan-cached query is more expensive than the uncached hinted one.** Not equal — worse, by
eight times the control floor, in every one of nine blocks.

Why: a hint restricts the index set before enumeration ("Hint by name specified, restricting
indices"), so planning a hinted query is already cheap. The unhinted query must instead discriminate
across all five indexes on the collection to build a cache key, then `planFromCache` and still
construct an executor. On this shape the cache costs slightly more than it saves.

### Three consequences

1. **The challenge does not apply to this operation.** A hinted baseline is not a flattering
   reference here, it is the *cheaper* one. The L4a envelope needs no discount.
2. **It independently corroborates closing the plan-cache lead**, by a completely different route
   and instrument. L2 said the ceiling for caching hinted plans is ≈−10.3%; L6 says that when
   caching is genuinely available on this shape it is worth **+0.43%**. A change that buys real plan
   caching for this query would make it slower.
3. **It explains the source document's unexplained result.** `get_children.md` §5 records "dropping
   the pinned hint: +0.30% client-side P50, neutral to negative" and could not say why, since
   dropping the hint should have bought plan caching. This is why: the caching is worth nothing, and
   the index discrimination it requires costs a little. Two instruments, two builds, 7.0.34 and
   master, +0.30% and +0.43%.

**Limits.** Instructions are not time and this delta is not converted to one. Five indexes on the
collection — a collection with fewer would discriminate more cheaply and the sign could flip. One
shape, one build.

## L7 — built it: express prefix scan, **−20.8% instructions / −35.5% server CPU / −18.5% wall**

Implemented, verified and measured. Branch `express-prefix-scan`, Draft PR
`carsontung666/mongo#6`. Artifacts `runs/getchildren_plancache_20260809/express_rot{0,1,2}.json`
and `express_instructions.json`.

### Result

Three campaigns, 20 blocks x 40 sweeps x 64 parents, gate rotated across three ports and three
byte-identical dbpath copies, on a box verified quiet first.

| instrument | effect | control floor |
|---|---|---|
| **retired instructions** | **−20.81%** (blocks [−22.20, −20.17]) | −0.16% |
| **server CPU** | **−35.48% / −35.44% / −36.09%** | +0.91% / −0.30% / +1.37% |
| **client wall** | −18.27% / −18.48% / −19.57% | — |

**60 of 60 blocks improved.** Absolute server CPU falls from 93.5–97.8 µs to 60.0–64.6 µs.

Instructions and CPU are separate quantities and are not combined. Instructions is the figure that
transfers between machines; CPU is what this server actually spends. **CPU falls further than
instructions** — about 35% against 21% — which means express also raises instructions-per-cycle by
roughly a fifth. The stage tree's virtual dispatch and `WorkingSet` indirection have worse locality
than straight-line code. Both numbers are real; neither is the "right" one on its own.

### Correctness gate, run before any number was believed

`bench/db/verify_express_prefix_scan.py`, gated against ungated on byte-identical data:
`explain` shows `EXPRESS_IXSCAN` against `PROJECTION_SIMPLE`/`FETCH`/`IXSCAN`; 660 rows over 64
parents identical element-wise and in order; absent parent returns nothing; a 5,000-row scan across
~49 batches identical with no duplicates; **the same under `internalQueryExecYieldIterations=1`**,
which yields between essentially every document; and five shapes the eligibility must refuse fall
back correctly. All passed.

The first of those is the one that matters most: without asserting express is actually selected,
every other check passes while measuring nothing.

### I was wrong about the size, in the conservative direction

L4a derived an envelope of ≈24 µs from the `_id` express-vs-hinted lever and said, in as many words,
that a bounded-scan fast path "would recover **less** than 24 µs — never more". **The measured
saving is ≈33 µs.** The reasoning error is worth recording because it was not a rounding matter:

the lever measured a **one-row** query, so it priced only the *fixed* per-command cost express
removes. For an eleven-row shape express additionally removes the per-row stage machinery — the
`PROJECTION_SIMPLE` and `FETCH` stages and the `PlanStage::work()` dispatch for every row. L3's own
profile had that in it all along: `PlanExecutorImpl::getNextBatch` at **14.87% inclusive**, with
`PlanStage::work` 13.82% and `ProjectionStage::doWork` 13.78% underneath. I read those as "the
per-row side is closed" when they were in fact more of what a fast path deletes.

So the transfer rule "fixed cost transfers between shapes" was right, but incomplete: the fast path
does not only remove fixed cost.

### What the change is

Most of it already existed, which is why it is small. `LookupViaUserIndex::consumeOne` already seeks
an equality *range* with an end position, and `PlanExecutorExpress::getNext` already loops. The
restriction was the iterator setting `_exhausted` after one document and rebuilding its cursor
locally each call.

Three things were needed beyond that:

1. **`PrefixScanViaUserIndex`**, a new iterator rather than a change to `LookupViaUserIndex`, so the
   point-lookup path is untouched and so this does not collide with the `get_node` agent's
   compound-equality work in the same class.
2. **No cursor across a yield.** `releaseResources()` drops it; the next call re-seeks past the last
   key returned. `PlanExecutorExpress::detachFromOperationContext` only swaps its `OperationContext`
   pointer and does not reach into the plan, so a held cursor would keep a dangling one. This is the
   hazard L4c flagged, sidestepped rather than solved.
3. **`stashResult()` implemented.** It was `MONGO_UNREACHABLE_TASSERT(8375808)` — sound for a plan
   that cannot overflow a batch, fatal for one that can. This is the concrete form of the
   ClientCursor problem the `get_node` agent identified, and why the shipped eligibility rejects
   `batchSize`.

### Limits

One shape, one workload, one build. Eligibility rejects `batchSize`, so a client that sets one gets
the old path — lifting that needs the ClientCursor question settled, not just `stashResult`.
Multikey indexes are refused outright rather than reasoned about. The resume semantics are "after
the last key returned", which matches an index scan across a yield but has not been tested against
concurrent writers beyond the yield-forcing test. No jstest yet.

## L8 — pre-PR checking found a wrong-results bug: **the hint was being ignored**

The A/B proved the change is fast on one shape. That is not the question that decides whether it can
be proposed. The question is whether turning the gate on changes the answer to *any other* query,
and answering it found a real defect.

### The instrument

`bench/db/fuzz_express_prefix_scan.py` — two mongods from one binary on identical data, differing
only in the gate, comparing **795 query shapes** element-wise. The data is chosen to be hostile
rather than representative: arrays (multikey), nulls, missing fields, dotted paths, empty strings,
values that compare equal across BSON types, and heavy duplication on the equality prefix. The
indexes deliberately include the kinds the eligibility is supposed to **refuse** — multikey, sparse,
partial, descending, collated, hashed — so a refusal that does not actually happen surfaces as a
wrong answer rather than as nothing.

29 of 795 differed, in two classes.

### Class 1 — my test was too strict, not the code

`sort=None` cases returned the same **set** in a different order. Without a sort MongoDB guarantees
no order, so this is legal. The fuzzer now compares unsorted queries as sets. Worth stating plainly
that express *does* reorder unsorted results, because it forces index order where the planner might
have chosen otherwise — legal, but observable.

### Class 2 — a real wrong-results bug

**Whatever index the query hinted, the gated path used `abcd`.**

| hint | sets equal? | index actually used |
|---|---|---|
| `partial_a_c` | **no — 63 rows against 53** | `abcd` |
| `sparse_opt` | yes, by luck | `abcd` |
| `multikey_arr_c` | yes, by luck | `abcd` |
| `dotted_subk_c` | yes, by luck | `abcd` |

`partial_a_c` carries `partialFilterExpression: {c: {$gt: 0}}`, so hinting it is a request for the
subset of documents that index contains. The fast path returned all 63 instead of 53. **Silently
wrong rows, no error.**

**Root cause: an assumption I made and never checked.** I believed `params.mainCollectionInfo.indexes`
was already narrowed to the hinted index, having seen "Hint by name specified, restricting indices"
in the server log. It is not — `QueryPlanner::plan` applies the hint *during planning*, which is
exactly the step this path skips. So the fast path had the full index list and picked its own.

Fixed: the candidate loop now skips any index that does not match the hint, using the same rule as
`QueryPlanner`'s own `hintMatchesNameOrPattern` ( `{$hint: <name>}` by name, otherwise key pattern).
A hint naming an index the eligibility refuses now falls through to normal planning.

Verified after the fix, on the real workload's shape:

| query | plan | index |
|---|---|---|
| no hint | `EXPRESS_IXSCAN` | `allops_tree_parent_path` |
| hint by name (the benchmark's own shape) | `EXPRESS_IXSCAN` | `allops_tree_parent_path` |
| hint by key pattern | `EXPRESS_IXSCAN` | `allops_tree_parent_path` |
| **hint a different index** | `PROJECTION_SIMPLE`/**`SORT`**/`FETCH`/`IXSCAN` | falls back |

The last row is the fix working: hinting an index that cannot provide the order falls back *and*
correctly grows a `SORT` stage.

Re-run after the fix: **795 shapes, no differences.**

### Why this one nearly escaped

Three of the four hinted cases returned the same set *by luck*, because those indexes happen to
contain every document. Only the partial index exposed it. A test that had used ordinary indexes
would have passed while the bug was live.

### A second issue found by reading rather than testing

The stash-serving block added to `PlanExecutorExpress::getNext` was placed **above** the scope that
starts the execution timer, checks the fail point, and calls `checkForInterrupt()`. With the gate off
`_stash` is always empty so it could never fire — but that is safety by luck. Moved inside the
guarded scope: returning a stashed document must not be a way to dodge a `killOp`.

### A second wrong-results bug, found by MongoDB's own test rather than by mine

Running `jstests/core/index/express*.js` against a gated and an ungated server found one regression:
`express_id_eq.js`, asserting

```js
// Assert that equality to null does not use express because 'null' isn't an exact bounds
// generating type.
assert(!isExpress(testDB, collection.find({_id: {$eq: null}}).explain()), ...);
```

`{$eq: null}` also matches a **missing** field, so its index bounds are a superset that a `FETCH` is
expected to re-filter — and this path does no re-filtering. My eligibility accepted any equality
value. The shipped express path had solved this already with
`Indexability::isExactBoundsGenerating`; I had not copied it. Fixed by using the same helper rather
than reasoning about which types are safe.

**The method lesson, which matters more than the bug.** My fuzz compared *results* over 795 shapes
and passed — on that data `{a: null}` genuinely returned the same rows, because every document had
the field present and a missing field indexes as null anyway. Only MongoDB's assertion about the
*plan* caught it.

> Comparing results shows a path did not go wrong **this time**. Asserting the plan shows it did not
> take a path that **can** go wrong.

The fuzz now carries plan assertions too, for six shapes that must not express.

### Both bugs came from the same place

`isEqualityExpressEligibleQuery` refuses **every hinted query and every sorted query** outright.
This change supports both — that is the entire point of it — and **each relaxation grew exactly one
wrong-results bug.** That is not a coincidence worth glossing: those two rules were load-bearing,
and relaxing a conservative rule means taking on the reasoning it was standing in for. Both fixes
ended up delegating to MongoDB's own helper for the thing the rule was protecting, which is where
they should have started.

### Non-intrusion audit

The whole change has **two deletions**: `isEOF()`'s body, and `stashResult()`'s
`MONGO_UNREACHABLE_TASSERT`. Everything else is additive, and `express_plan.h` is purely so. With the
gate off the surface is three lines, each individually arguable: `_stash` is always empty, so
`isEOF()` is unchanged, `getNext()`'s new block never runs, and `stashResult()` was previously
unreachable.

### Final state, after both fixes

| check | result |
|---|---|
| `jstests/core/index/express*.js` | **0 regressions** (`express_id_eq.js` passes again); 3 fail on both servers under this standalone harness, which lacks resmoke's fixtures |
| differential fuzz | **795 shapes, no differences**, plus 6 plan assertions |
| correctness gate | all pass, incl. 5,000 rows over ~49 batches and `internalQueryExecYieldIterations=1` |
| collated collection | declines correctly, including an index opting out with `{locale: "simple"}` |
| express unit tests | **30/30** |

Re-measured on a quiet box after the fixes — slightly *better* than before them, because the hint
check short-circuits the candidate loop earlier:

| instrument | effect | control floor |
|---|---|---|
| retired instructions | **−22.79%**, blocks [−22.85, −22.71] | −0.57% |
| server CPU | **−36.73% / −36.44% / −34.99%** | −0.28% / +0.01% / −0.42% |
| client wall | −19.39% / −18.61% / −17.27% | — |

**60 of 60 blocks improved.** Absolute server CPU 89.1–93.2 µs falls to 58.0–59.5 µs.

## L9 — the express change was wrong when first measured; five defects, all found by checking

The −35% in L7 was measured against an implementation that **silently dropped rows and never
yielded**. An adversarial review found it; I reproduced every claim before accepting it. This is the
most important entry in this file, because the number looked excellent and the change was broken.

### The one that mattered

**Silent data loss on duplicate index keys.** The resume point stored
`getKeyStringWithoutRecordIdView()`. For a standard index the RecordId **is** part of the stored
key, so re-seeking exclusive-after that value steps past *every* entry sharing it.

    500 documents, one identical index key, find({a:1,b:2}).sort({c:1,d:1})
      gated   101 / 500      <- the first batch, then nothing. No error.
      ungated 500 / 500

Every getMore boundary landing inside a run of duplicate sort keys dropped the remainder.

**No test I had written could have caught it.** The workload is a tree: every child has a distinct
sort key, so a run of duplicate keys never existed in any of my data. "5,000 rows across ~49
batches, no duplicates" proved the easy case and read like proof of the hard one.

Fixed by keeping the cursor and using `SortedDataInterface::Cursor`'s own `save()`/`restore()` plus
`detach`/`reattach`, plumbed through `PlanExecutorExpress` — which is what `IndexScan` does, and
what I had talked myself out of. My stated reason for hand-rolling a resume ("detachFromOperationContext
only swaps a pointer") described a two-line fix, not a reason to reimplement cursor positioning.
**500/500 and 700/700 after.**

### The other four

| defect | why it mattered |
|---|---|
| `cq->getCollator()` read in the same call that `std::move`s `cq` | argument evaluation is unsequenced; right-to-left ordering is a null deref. The sibling factory hoists into locals for exactly this reason and I had dropped that guard |
| no `PlanYieldPolicy` | harmless while an express plan returned one document; an iterating one can hold a storage snapshot for an unbounded getMore. Now yields on `PlanYieldPolicyImpl`'s cadence and knobs |
| `std::deque` for the stash | allocates **twice** on default construction (measured: 2 allocs / 576 bytes, against 0 for `vector`), so every `_id` point lookup paid two mallocs it did not before — *with the gate off*. I introduced this myself while "improving" the code |
| `getenv` feature gate | not settable at runtime, invisible to `getParameter`, unreachable from any test suite. Now the `internalQueryEnableExpressPrefixScan` server parameter |

### `internalQueryExecYieldIterations` defaults to −1

Worth its own line because I reported it as evidence twice. Iteration-based yielding is **off by
default**; the 10 ms `internalQueryExecYieldPeriodMS` is what actually fires. Setting the iterations
knob to 1 and claiming "yields between essentially every document" tested nothing at all — and
express never instantiated a yield policy to read it in the first place.

### What my testing actually failed at

Four distinct failures, each with a different cause, worth separating:

1. **No duplicate sort keys anywhere in the data.** Tree data cannot produce them. The hard case has
   to be constructed on purpose; it will never appear by accident.
2. **A vacuous yield test.** I set a knob that neither controls anything by default nor is read by
   this code path, and reported the result as evidence.
3. **A regression I introduced while cleaning up.** `vector` → `deque` for an O(1) pop, on a path
   where the container is always empty and the allocation is the entire cost.
4. **Comparing results where I should also have asserted plans** — see L8.

### Now

`jstests/core/index/express_prefix_scan.js` covers duplicate keys spanning batches, hints honoured
including a partial index falling back, `{$eq: null}` refused, multikey and sparse refused, and runs
every query with the knob off and on comparing element-wise. **Verified the test can fail**:
inverting one assertion exits 253.

| check | result |
|---|---|
| duplicate keys, 500 identical / 700 in runs of 37 | **500/500, 700/700**, no duplicates |
| differential fuzz | **797 shapes, no differences** |
| new jstest | passes, and demonstrably fails when broken |
| `jstests/core/index/express*.js` | 0 regressions |
| express unit tests | 30/30 |

Performance after the fixes: **retired instructions −22.87%**, blocks [−22.93, −22.84], floor
−0.02% — essentially unchanged from before them, because the resume path only runs at getMore
boundaries and an eleven-row query never reaches one. Server CPU is about −36%, 20/20 blocks in each
rotation, but that run's control arm was noisy (spikes to +99%), so it is being re-measured on a
verified-quiet box before being quoted as final.

## Still to run

- **L4** — build the express extension per L4c. Envelope ≈24 µs (L4a), shape known (L4b), driver
  already supports it (L4c), hazard identified (yield/restore of a persisted cursor). Belongs to
  whoever owns `src/mongo/db/exec/express/`; the `get_node` agent has been sent L4a and L4b.
- **L5** — the real M1 mechanism. **Not planned.** L2 priced it at a ≈−10.3% ceiling that a correct
  build lands below, with less margin than the run-to-run floor. Closed, not deferred.

---

## Summary of the lane

| lead | outcome |
|---|---|
| L0 | The brief's premise is 7.0.34's. Master dropped the SBE carve-out; the exclusion is now unconditional |
| L1 | Relaxing the exclusion alone is **inert** — every path into the classic cache runs off a `MultiPlanStage` |
| L2 | Ceiling for caching hinted single-solution plans: **≈−10.3% server CPU**, an upper bound |
| L2a/L2b | Two wasted builds: master plans in a different file, and CBR owns its own single-solution exit |
| L2e | Review found two wrong-results defects and one false claim of mine. All fixed |
| L3 | **No hotspot exists.** Largest leaf 2.10%; allocator traffic 8.53% is the largest mechanism |
| L4a | A fast path is worth **≈24 µs per command**, measured on a different shape |
| L4b/L4c | Express already range-seeks; the restriction is two lines. Plan and hazard written up |

**What MongoDB should take from this lane:** the plan-cache change is not worth building, and the
reason is not that caching is cheap — it is that planning is only about a tenth of this operation,
while 71% is fixed per-command cost with no concentrated term anywhere in it. The one change with
room above the bar is extending express to a bounded prefix scan, and the machinery for it is
almost entirely present already.
