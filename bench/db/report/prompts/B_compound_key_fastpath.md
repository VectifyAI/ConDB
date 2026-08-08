# Task B — Give a unique compound-index equality the fast path `_id` already gets

MongoDB is paying for this work; the deliverable is a change to `mongod`. This one is specifically a
**JSON-tree-shaped gap**: tree nodes are naturally keyed by `(tree_id, node_id)`, not by `_id`.

## What is wrong today

MongoDB gives an `_id` equality a fast path that skips plan selection and executor construction
entirely — `IDHACK` on 7.0.34, `EXPRESS` on 8.0+. It gives the *semantically identical* lookup on a
unique compound index nothing.

On the measured dataset, `allops_tree_node` is a **`unique` index on `(tree_id, node_id)`** —
verified live — and the query binds both fields with equalities, so the seek can match at most one
key and at most one document. It is a point query in every sense that matters. It still pays full
planning on every call, because:

- `IDHACK` is `_id`-only.
- On `upstream/master`, `collectExpressEqualities()` in `src/mongo/db/query/query_utils.h` requires
  the predicate's match type to be a single `EQ`, so a conjunction of equalities falls through to
  regular planning even when a unique compound index binds every one of them.

Measured cost of not having the fast path, on the real 10M-node collection, paired 14 blocks
(`bench/db/report/evidence/review_20260807/v6_idhack.json`): an `_id` equality without a hint costs
**−36.7% server CPU** (block min/max [−40.3, −29.9], 14/14 blocks) and **−17.4% wall** (14/14)
against this same query — **26.3 µs of 71.7**.

**Read that figure carefully.** It measures the fast path *on an `_id` lookup*. Transferring it to a
compound-key eligibility rule assumes the work saved is the same — true of plan selection and
executor construction, but **not measured on a compound-key implementation**. Establishing the real
number is the first half of your job.

## There is already an implementation, and it has not been verified

The fork carries commit **`4fb23d8d1ba` "Extend the express fast path to compound equality
predicates"**, one commit ahead of `upstream/master`. Find the branch with
`git branch --contains 4fb23d8d1ba`. What it claims to do:

- generalise `collectExpressEqualities()` to admit a conjunction when every child is an equality
  generating exact bounds and no two constrain the same path;
- add `orderEqualitiesForIndex()` to line those operands up against a candidate index, rejecting it
  unless the index's leading fields are exactly the constrained paths — an unconstrained leading
  field would leave keys the express executor cannot filter — and rejecting multikey;
- change `LookupViaUserIndex` to take a vector of operands instead of one element, which the commit
  message argues needs no new execution machinery because it already appends the equality and leaves
  each remaining index field fully open.

**It has been through none of this project's acceptance gate**: no independent review, no proof of
effect on the real dataset, no proof of non-intrusion, and none of the numbers above were measured
against it. Treat it as a starting point, not as a result. **Do not trust its commit message** — this
project has had commit messages with wrong numbers more than once. Re-derive everything.

Your job is to make it correct and to establish what it is actually worth, or to show that it is
wrong and say why.

## Two constraints that must survive into the design

Both verified live on 7.0.34:

1. **A hint disqualifies the fast path.** `query_utils.cpp:52-59` requires
   `findCommand.getHint().isEmpty()`. Production tree workloads pin hints for plan stability, so a
   fast path a hint disables is unavailable exactly where plan stability matters. **Whether the hint
   check can be relaxed when the hinted index is the index the fast path would choose is an open
   question this evidence does not settle** — it is worth answering, and it may be the more valuable
   half of the task.
2. **A sub-document `_id` matches by exact document equality including field order.**
   `{_id: {tree_id, node_id}}` returns one document and `{_id: {node_id, tree_id}}` returns **none**,
   both taking `IDHACK`, with no error and no plan difference. A dotted-path rewrite is not an escape
   — it falls back to `COLLSCAN`. This is a correctness argument for the compound-key path
   independent of performance: it lets a user express the natural key without the ordering hazard.

## Environment

- Fork: `/home/junyao/code/mongo`. Remotes: `origin` = `git@github.com:carsontung666/mongo.git`,
  `upstream` = mongodb/mongo. Pinned base `0561c098b99ac5e929005e70a2e37d7a97a82423`.
- Build: `bazel build --config=opt //src/mongo/db:mongod`, ~8 min cold, 96 cores.
- Tests: `resmoke`. Express has existing coverage — `jstests/core/query/express.js` and the
  `idhack.js` / `profile_find.js` / clustered-collection tests have all been touched by earlier work
  in this project, so read what is there before adding.
- 7.0.34 source for reference: `/home/junyao/code/mongo-r7.0.34`. **The change targets master**; the
  measured baseline server is stock 7.0.34 on `mongodb://localhost:57017` (db `bench`,
  `layout2_view`, 10M docs, no auth) and has no express at all, so those figures are context.
- Workload: `/home/junyao/code/pageindex/ConDB/bench/db/bench_all_ops_layouts.py`. Read it.
- Prior evidence: `bench/db/report/ops/get_node.md` and the artifacts it cites.
- A single-binary A/B campaign runner exists: `bench/db/condb_ab_campaign.py`.

## Measurement discipline — not optional

Five failure modes have recurred here. Check against all five.

1. **Unit mixing.** Server CPU, client wall, retired instructions are three quantities.
   `planningTimeMicros` is wall; `cpuNanos` is CPU. Never compare across them.
2. **Unpaired arms.** Alternate within blocks, report per-block paired deltas.
3. **Inclusive/exclusive confusion** in profiles. Never add sibling inclusive percentages.
4. **Fabricated ceilings.** Do not fill a ceiling column with the measured value.
5. **Non-like-for-like arms.** Verify output equality element-wise, every block.

Plus: **single binary** with the change gated on an environment variable read once at startup, so
build-to-build code-layout variance (2.6 percentage points on identical source) cannot enter the
ratio; a **control endpoint** the gated code provably cannot fire on; an **activation counter**
printed at exit, reported as fired/total. **Never benchmark while anything is compiling.** Announce
dataset, duration and load before heavy runs. Report the spread you observe and never claim an effect
smaller than it — note that fresh connections to the same `mongod` differ by 14–26% in P50 on this
two-socket box, so hold a connection fixed across arms.

## Acceptance gate

1. A test that **fails on the unpatched base**, artifact retained.
2. **Proof of non-intrusion**, including `internalQueryExecYieldIterations: 1`.
3. **Locally-decidable safety** — parent-tells-child is fine, a global assumption is not. Pay
   particular attention to multikey, collation, and partial/sparse indexes: a "unique" index that is
   partial does not guarantee at most one match for an arbitrary equality.
4. **A blank-context agent review** to MongoDB query-team PR standards. Fix what it finds.
5. Honest limits stated, including what you did not measure.

## Deliverables and constraints

- A branch pushed to `origin`, with a **Draft** PR.
- **There is no real SERVER- ticket. Do not invent one. Do not claim upstream-ready.**
- No AI-authorship traces in commits or PR text; this goes to MongoDB engineers.
- Never push the ConDB repo — commit there locally only.
- Say plainly, with `file:line`, anything you could not do.

## What would make this not worth shipping

The eligibility check runs on every `find`, so a rule that costs more to evaluate than it saves on
the queries it accepts is a net loss — measure the miss path, not only the hit path. Other candidates:
uniqueness cannot be relied on without also proving the index is neither partial nor sparse nor
multikey, and the resulting condition is so narrow it rarely fires; or the existing planner already
short-circuits enough of this that the measured win is single digit. This project does not ship
single-digit percentages — if that is what it turns out to be, say so and stop.
