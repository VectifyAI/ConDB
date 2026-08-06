# Prompt for a fresh attempt at the two unsolved operations

Paste everything below the line into a new session. It is written to be self-contained.

---

You are looking for a narrow, upstream-reviewable performance optimization in the MongoDB server.
Two previous attempts failed for reasons that are documented below. Your job is to find a **different
approach** — not to redo either of them.

## Codebase

`/home/junyao/code/mongo`, pinned base commit `0561c098b99ac5e929005e70a2e37d7a97a82423`. Read files
with `git -C /home/junyao/code/mongo show 0561c098b99a:<path>`. Branch off that commit; do not
disturb existing branches. Note the tree layout: classic stages are under
`src/mongo/db/exec/classic/`, key_string under `src/mongo/db/storage/key_string/`, the matcher
parser under `src/mongo/db/query/compiler/parsers/matcher/`.

Build with `bazel build --config=opt //src/mongo/db:mongod` (about 8 minutes cold). Benchmarks live
in `src/mongo/db/query/*_bm.cpp` and report retired user-space instructions via a PMU wrapper.
resmoke needs the repo venv: `./.venv/bin/python3 buildscripts/resmoke.py run --suites=core ...`.

## The two workloads still unsolved

Both hit the **classic** engine (`internalQueryFrameworkControl` defaults to `kTrySbeRestricted`,
and these have empty pipelines, so SBE is not involved).

**A. Child expansion.** `find({parent_id: X})` on a non-unique index over `parent_id`, returning
whole documents. Fan-out is small — single digits to low tens. Plan is `IXSCAN → FETCH`.

**B. Subtree retrieval.** `find({dfsOrdinal: {$gte: a, $lt: b}})` on an index over `dfsOrdinal`,
returning either whole documents or a covered projection of metadata fields. Result sets from a
handful to tens of thousands. Uses the single-interval scan with `setEndPosition`; covered
projections get `ProjectionNodeCovered`.

## What has already been tried, and exactly why it failed

**Do not re-propose either of these.** Both are implemented, tested and recorded.

1. **Skipping index-key BSON materialization when the only consumer is a FETCH.**
   `IndexScan` decodes every key to BSON; on an `IXSCAN → FETCH` plan nobody reads it, because
   `WorkingSetCommon::fetch` clears `keyData` after its post-yield consistency check. A change was
   built where `FetchStage` tells a direct `IXSCAN` child, `IndexScan` uses `nextKeyString()` and
   stores a `key_string::Value`, and the check compares KeyStrings directly.

   It is **correct** — it fires 217/217 on `IXSCAN → FETCH`, 0/127 on covered projections, and 42
   core jstest entries pass with `internalQueryExecYieldIterations: 1`. It has **no measurable win**.

   The reason is the important part: the storage layer's `KeyInclusion::kExclude` wins by *producing
   nothing* (measured: 47.6% and 52.9% off cursor advance, via the existing A/B in
   `sorted_data_interface_bm.cpp:249-252`). But a FETCH needs per-key data to survive across `work()`
   calls, so anything of this shape must retain something — `getValueCopy()`
   (`sorted_data_interface.h:644-647`) allocates and copies. It trades a BSON build for a KeyString
   copy. **The storage-layer ceiling does not transfer to a FETCH plan.** If your idea requires
   retaining per-key state across `work()` calls, it will hit the same wall.

   Branch `agent/condb-ixscan-key-exclusion`; evidence in
   `bench/db/report/evidence/mongodb_indexscan_key_exclusion_20260806/` in
   `/home/junyao/code/pageindex/ConDB`.

2. **Letting a positive `batchSize` keep EXPRESS eligibility** (a different operation, point lookup,
   listed so you do not rediscover it). Worth 37.8% fewer instructions, but blocked:
   `jstests/core/administrative/profile/profile_find.js:50` says `// Use batchSize to avoid express
   path` and asserts `planSummary`/`queryHash`/`planCacheKey`. It is a documented escape hatch a core
   test depends on, and removing it changes cursor lifecycle. That is a product decision, not a
   coding problem.

## What has been checked and found genuinely optimal — verify before re-treading

- **The document body is copied exactly once** end to end: `RecordData::releaseToBson()` returns a
  view (`record_data.h:49-51`), `resetDocument` reuses `DocumentStorage` (`working_set.cpp:163-168`),
  `Document::toBson()` is trivially convertible (`document.h:345-348`), and the single copy is the
  append into the reply buffer. `getOwned()` on the find path is only in the yield handler.
- **Per-`work()` timing is off by default** (`plan_stage_timer.h:27`).
- **`getNextBatch`** (`plan_executor_impl.cpp:698-801`) already avoids per-document `PlanExecutor`
  overhead, and `getPostBatchResumeToken()` returns a static empty object for non-collscans.
- **Per-execution planning is architectural.** Single-solution classic plans are deliberately not
  cached (`get_executor_helpers.cpp:220-238`, `plan_cache/README.md:307-318`, SERVER-13341,
  SERVER-90880). Do not propose "cache them" — that is a known large project, not a narrow PR.
- **Express cannot iterate**, by design, with `TODO SERVER-87016`
  (`plan_executor_express.cpp:1002-1014`). Making it iterate is a redesign.
- **KeyString has no partial-decode API** and cannot get one cheaply: skipping a component
  desynchronizes `TypeBits::Reader` (`key_string.h:314`, `key_string.cpp:2098`, `:3006-3008`).

## Where to look instead — suggestions, not instructions

Nobody has examined these closely. Treat them as starting points and follow the code:

- The **covered projection path** for workload B: `KeyString → full key BSON → projected BSON` is two
  builder passes (`wiredtiger_index.cpp:1009`, then `projection.cpp:259-279`). Unlike the FETCH case,
  the projection consumes the key *immediately* and retains nothing, so the "must retain state" wall
  above does not apply here. This is the most promising untried lead.
- Per-batch and per-document work in the **command layer**: `CursorResponseBuilder`, BSON assembly,
  the 16 MB check (`find_common.h:132-134`), reply construction.
- **Repeated plan-time constants recomputed per call**, e.g.
  `express_plan.h:574-581` rebuilds a `StringSet` and re-derives `Ordering::make(keyPattern)` on every
  invocation.
- Allocation churn: `PreallocatedContainerPool` usage, `SharedBufferFragmentBuilder` construction
  inside loops (`working_set_common.cpp:158-159` does this per key on the post-yield path).
- Anything you find yourself — the list above is not exhaustive and was not written by someone who
  had solved the problem.

## Measurement discipline — this is not optional

A previous attempt published wrong numbers twice. Both times the cause was measurement, not code.

1. **Build-to-build variation on identical production source reached 2.6 percentage points** on one
   query benchmark. A single base-vs-patched pair proves nothing at the few-percent level.
2. Every performance claim needs a **control benchmark on which the change provably cannot fire**,
   run on the same two binaries. If the control moves as much as the treatment, you have measured
   the build, not the change.
3. **Re-run the pair** and confirm the ratio reproduces before quoting it.
4. **Never benchmark while the machine is compiling.** One set of published figures was roughly
   double the true effect for this reason alone.
5. Prove the optimization **actually fires** — a temporary counter printing fired/total per plan is
   cheap and settles it. A change that never executes measures as pure noise and can look like a win.
6. Report medians or means explicitly, and do not fit a fixed-plus-per-element decomposition to three
   data points.

## What counts as done

- The change is minimal and its safety condition is decided **locally** where possible. The pattern
  that passed review twice is parent-tells-child (`CountStage` → `CountScan`,
  `FetchStage` → `IndexScan`), not a planner flag that a future change could set wrongly.
- A test that **fails on the unpatched base** — verify by reverting the production hunks, rebuilding,
  and capturing the output as an artifact. Asserting a pass count without a retained log is not
  evidence.
- Non-intrusion: relevant dbtest suites, plus core jstests, plus a run with
  `internalQueryExecYieldIterations: 1` if you touch anything on the yield path.
- Honest limits stated: retired instructions are not latency; one host; hint-forced plans; no
  sharded, 7.0.34 or production claim.

If after genuinely trying you conclude there is nothing narrow left, say so with `file:line` evidence.
A concrete negative result is worth more than a speculative positive one — attempt 1 above is a
negative result and it is the most useful thing in this document.
