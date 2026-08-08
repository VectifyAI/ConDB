# Task A — Make `mongod` transmit a reply before the batch is full

This is the largest single optimization identified in this project. MongoDB is paying for this work;
the deliverable is a change to `mongod`, not advice to an application.

## What is wrong today

`mongod` assembles a `find`/`getMore` batch **to completion** before any of it reaches the transport.
`src/mongo/db/query/find_common.cpp:63` sets `kMaxBytesToReturnToClientAtOnce` to
`BSONObjMaxUserSize` (16 MB); the fill loops are `find_cmd.cpp:653` and `getmore_cmd.cpp:388`; the
reply is handed to the transport only after the command returns. PostgreSQL emits each row as a
`DataRow` as it is produced, so its server and client costs overlap. MongoDB's do not.

Measured on a 10M-node JSON tree, reading a whole subtree. Socket-level instrumentation, no CPU
accounting involved — "first-byte wait" is time the client is blocked before *any* byte of a reply
has arrived. Round-trip counts are `serverStatus.metrics.commands` deltas, not client inference:

| rows | wall ms | first-byte wait | share of wall | `find` + `getMore` |
|---|---|---|---|---|
| 11,686 | 20.19 | 9.08 | **45.0%** | 1 + 1 |
| 96,238 | 142.11 | 61.07 | **43.0%** | 1 + 3 |
| 1,404,566 | 2127.09 | 849.22 | **39.9%** | 1 + 37 |

At the cohort median the whole 5.11 MB result arrives in **two round trips**, so there is nothing to
overlap. MongoDB's server does only 18% more CPU work than PostgreSQL's on this operation but takes
43% more wall time; the difference is serialization, not work.

**The machinery already exists.** `src/mongo/transport/session_workflow.cpp:292`,
`makeExhaustMessage`, sets `kMoreToCome` (at `:328`) and makes the server keep producing without
waiting for a `getMore`. It fires only when the client requests an exhaust cursor, and exhaust is
refused behind `mongos` (`pymongo/synchronous/cursor.py:253`, `:398`), incompatible with automatic
encryption (`:1099`), and cannot be combined with `limit()` (`:456`). So the capability is in the
server and is reachable only through a client option most production deployments cannot use. That
exclusion is the whole argument for doing this server-side.

## The bound, and what it is a bound on

Driving the existing exhaust path from the client as a proxy for the change, over a full 200-subtree
cohort with one fixed batch size for every input, arms interleaved with rotated order, element-wise
fingerprint check on all 200 inputs, two independent passes:

- **−40.5% cohort-weighted against MongoDB's own baseline** (pass 2: −41.7%)
- **−28.2% at the per-subtree median**
- −40 to −46% on individual large reads

Those two figures answer different questions and this project has already made the mistake of
quoting one under the other's name. Say which you mean, every time.

The proxy moves two levers at once — exhaust *and* an explicit `batchSize`. Exhaust alone is +2.0% at
the median but −30.3% at 96,238 rows and −45.0% at 1.4M rows, so at the tail exhaust alone captures
essentially all of it; near the median there is only one `getMore` to overlap, which is why batch
size matters there. **A streaming server would plausibly make batch size moot, but nothing measured
so far establishes that** — establishing it is part of your job.

Cost side, measured on the proxy: per-operation server CPU rises 4.6–9.3% single-client. Throughput
gain by client count is **+42.0% at 1 client, +36.9% at 8, +5.0% at 32**, while aggregate server CPU
at 32 clients rises 22.7 → 30.2 cores. Both concurrency runs capped inputs at 30k rows, so **the tail
where the benefit lives was never run concurrently.** If your change makes it into a measurable
state, running that is worth more than another latency number.

## Environment

- Fork: `/home/junyao/code/mongo`. Remotes: `origin` = `git@github.com:carsontung666/mongo.git`
  (yours), `upstream` = mongodb/mongo. Pinned base `0561c098b99ac5e929005e70a2e37d7a97a82423`.
  Branch off that base; do not build on another agent's branch.
- Build: `bazel build --config=opt //src/mongo/db:mongod`, roughly 8 minutes cold on this box
  (96 cores).
- Tests: `resmoke`.
- 7.0.34 source for reference: `/home/junyao/code/mongo-r7.0.34`. **The change targets master.** The
  measured baseline server is stock 7.0.34 on `mongodb://localhost:57017` (db `bench`, collection
  `layout2_view`, 10,000,000 documents, no auth) — you cannot install your build there, so your A/B
  runs use your own build and the 7.0.34 figures above are context, not the arm you compare against.
- Workload definitions: `/home/junyao/code/pageindex/ConDB/bench/db/bench_all_ops_layouts.py`. Read
  it; do not guess the query shape.
- Prior evidence: `/home/junyao/code/pageindex/ConDB/bench/db/report/ops/get_subtree.md` and the
  artifacts it cites under `bench/db/report/evidence/` and `bench/db/runs/`.
- A single-binary A/B campaign runner exists: `bench/db/condb_ab_campaign.py`.

## Measurement discipline — not optional

Five failure modes have recurred in this project. Check your work against all five before writing
anything down.

1. **Unit mixing.** Server CPU, client wall time and retired instructions are three different
   quantities. `planningTimeMicros` is wall (tickSource); `cpuNanos` is CPU. Never divide, subtract
   or compare across them. This has happened five times here.
2. **Unpaired arms.** Alternate arms within each block and report per-block paired deltas. An
   unpaired −14% in this project became +0.5% under a paired design.
3. **Inclusive/exclusive confusion** in call-graph profiles. A plan-root frame's inclusive share
   contains its children's; never add sibling inclusive percentages.
4. **Fabricated ceilings.** Do not fill a "ceiling" column with the measured value.
5. **Arms that are not like-for-like.** Verify output equality element-wise, every block.

Additionally:

- **Single binary.** Compile the change in but gate it on an environment variable read once at
  startup, so both arms share one executable and build-to-build code-layout variance — measured at
  2.6 percentage points on identical source — cannot enter the ratio.
- **Control endpoint.** A workload in the same process on which the gated code provably cannot fire.
- **Activation counter.** Print fired/total at process exit via a static destructor, and report it.
  A change that measures well but never fired is the failure this catches.
- **Never benchmark while anything is compiling.** The box is shared. Announce dataset, duration and
  load before running anything heavy.
- Report the run-to-run spread you observe. This operation has a documented spread of roughly 24% at
  the median input, and it is input-dependent (12.7% at 96,238 rows). Never claim an effect smaller
  than the spread you measured.

## Acceptance gate

Before you call this done:

1. **A test that fails on the unpatched base.** Retain the artifact showing the failure.
2. **Proof of non-intrusion** — the change must not alter behaviour where it does not fire. Include
   `internalQueryExecYieldIterations: 1` in that check.
3. **Locally-decidable safety.** A parent stage telling a child what it may assume is acceptable;
   a global assumption is not.
4. **A blank-context agent review** to MongoDB query-team PR standards. Fix what it finds; do not
   argue with it.
5. **Honest limits stated** — what you did not measure, and what would falsify the result.

## Deliverables and constraints

- A branch pushed to `origin`, with a **Draft** PR.
- **There is no real SERVER- ticket. Do not invent one. Do not claim the change is upstream-ready.**
- Commit messages and PR text carry no AI-authorship traces; this goes to MongoDB engineers.
- Never push the ConDB repo (`/home/junyao/code/pageindex/ConDB`) — commit there locally only.
- If you cannot do something, say so plainly with `file:line` evidence rather than working around it
  silently.

## What would make this not worth shipping

Say so early if you find it. Candidates: the reply builder cannot be made incremental without
changing `CursorResponse`'s contract in a way that breaks drivers; partial transmission breaks error
reporting, because a command that has already sent bytes cannot then return an error status; the
overlap gain is eaten by per-chunk framing overhead; or the change regresses small results, which are
the overwhelming majority of real `find` traffic. A well-measured "this cannot be done without X" is
a better deliverable than a patch that only helps a benchmark.
