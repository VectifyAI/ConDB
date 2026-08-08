# Optimize MongoDB for `get_subtree` — retrieve an entire subtree

You own this operation end to end. MongoDB is paying for this work; every deliverable is a change to
MongoDB's own code. **This document gives you an initial plan. Execute it first. If its measured
effect is insufficient, do not stop — continue down the fallback leads, and past them if necessary,
under the same discipline.**

Three sibling agents work the other operations on this box. Your lane is **cursor batching / reply
transmission** (`find_cmd.cpp`, `getmore_cmd.cpp`, `session_workflow.cpp`) and, in the fallbacks,
the **KeyString decode path**. Express eligibility belongs to the `get_node` agent, the plan cache
to `get_children`, the driver to `get_entity` — do not modify those areas, and coordinate builds
(see Discipline).

## The operation and the gap

Hinted root lookup + hinted range scan over the exactly-covering index
`(path, node_id, title, summary)`, projecting `{node_id, title, summary}`, `PROJECTION_COVERED →
IXSCAN`, no FETCH (`totalDocsExamined: 0`), no SORT. **MongoDB 18.1 ms vs PostgreSQL 13.6 ms P50,
1.33× — the only gap measured in milliseconds.** Cohort of 200 subtrees, rows 5,006 → 1,404,566;
the 18 inputs above 100k rows carry 68.9% of MongoDB's cohort time (and 69.6% of PostgreSQL's).

At the cohort P50 input (11,686 rows): wall 14,127 µs vs 9,858; server CPU 8,330 vs 7,050. **The
server does 18% more CPU work but the operation takes 43% more wall time.** The difference is not
work — it is that server and client never run at the same time. Full analysis:
`/home/junyao/code/pageindex/ConDB/bench/db/report/ops/get_subtree.md`. Read it.

## Initial plan: make `mongod` transmit the reply before the batch is full

`mongod` fills a `find`/`getMore` batch **to completion** before any of it reaches the transport:
`src/mongo/db/query/find_common.cpp:63` sets `kMaxBytesToReturnToClientAtOnce` to
`BSONObjMaxUserSize` (16 MB); fill loops at `find_cmd.cpp:653` and `getmore_cmd.cpp:388`; the reply
is handed over only after the command returns. PostgreSQL streams each row as it is produced. At
P50 the whole 5.11 MB result arrives in **two round trips** — nothing overlaps.

Socket-level measurement (no CPU accounting; "first-byte wait" = client blocked before *any* reply
byte; round trips from `serverStatus.metrics.commands` deltas —
`bench/db/report/evidence/review_20260807_subtree/`):

| rows | wall ms | first-byte wait | share | find+getMore |
|---|---|---|---|---|
| 11,686 | 20.19 | 9.08 | **45.0%** | 1 + 1 |
| 96,238 | 142.11 | 61.07 | **43.0%** | 1 + 3 |
| 1,404,566 | 2127.09 | 849.22 | **39.9%** | 1 + 37 |

**The machinery already exists.** `src/mongo/transport/session_workflow.cpp:292`
(`makeExhaustMessage`, sets `kMoreToCome` at `:328`) makes the server produce without waiting for a
`getMore` — but only when the client requests an exhaust cursor, and exhaust is refused behind
`mongos`, incompatible with auto-encryption, and cannot combine with `limit()`. The capability is in
the server and unreachable for most production deployments. That is the argument for doing it
server-side.

The bound, from driving exhaust + `batchSize` as a proxy (200-subtree cohort, fixed batch 1000,
arms interleaved, element-wise output check, two passes): **−40.5% cohort-weighted against
MongoDB's own baseline** (pass 2 −41.7%), −28.2% per-subtree median, −40 to −46% on large reads.
Cohort-weighted it flips MongoDB from +48.9% slower than PostgreSQL to −11.4% faster — but the flip
is tail-carried (drop the 3 largest inputs → −0.9%) and the median request stays 8–10% slower. The
proxy moves two levers; exhaust alone is +2.0% at P50 but −45.0% at the tail. **Whether a streaming
server makes batch size moot is unestablished — establishing it is part of your job.**

Costs measured on the proxy: single-client server CPU +4.6–9.3% per op; throughput +42% at 1
client, +36.9% at 8, +5.0% at 32 (a saturation curve, not zero) while aggregate CPU rises 22.7 →
30.2 cores; **no input above 30k rows was ever run concurrently** — running the tail under load is
worth more than another latency number.

Things that could make this unshippable — find out early: `CursorResponse`'s contract may not admit
incremental building without breaking drivers; a command that has sent bytes cannot then return an
error status; per-chunk framing may eat the overlap; small results (the overwhelming majority of
real `find` traffic) must not regress.

## If the effect is insufficient, continue — in this order

The bar: no single-digit percentages.

1. **Fuse the covered-projection decode.** `KeyString::toBson` (24.4%) +
   `ProjectionStageCovered::transform` (12.3%) = **36.6% of server CPU** — the index walk itself is
   only 7.9%. The current path decodes every key component into an intermediate BSON object and
   immediately discards it; sequential *traversal* is required (skipping desynchronizes
   `TypeBits::Reader`), sequential *materialization* is not. A prior implementation exists at
   `bench/db/report/evidence/mongodb_recheck_20260806/covered_fused/`
   (`key_string::toBsonProjectedSafe()`) measuring **−8.3% retired instructions / −4 to −6% CPU
   time on a master-only microbenchmark, never on this operation** — treat as a starting point, do
   not trust its numbers, measure on the real shape. Also note MongoDB's scan decodes four key
   components where PostgreSQL reads three keys + two payload columns
   (`BOTTLENECK_20260806.md:450-458`) — part of the 36.6% is that extra component.
2. **Non-key payload columns in indexes** — PostgreSQL's `INCLUDE` equivalent, attacking the same
   36.6% structurally. This is a storage-format feature, not a patch; if you take it on, scope a
   design + a ceiling probe rather than a full implementation, and say what it would be worth.
3. If you find a better direction, take it — same discipline, same bar.

Ruled out already — do not re-litigate: `RawBSONDocument` (2.4× slower), wire compression on
loopback (snappy +58%, zstd +84%, zlib +1255%), parallel cursors (2 points for +50% CPU),
aggregation reshaping (every variant slower), `batch_size` alone at default exhaust (input- and
value-dependent, see the analysis), plan caching (planning is ~157 µs of 14.1 ms here).

## Environment

- Fork `/home/junyao/code/mongo`; `origin` = `git@github.com:carsontung666/mongo.git`, `upstream` =
  mongodb/mongo. Pinned base `0561c098b99ac5e929005e70a2e37d7a97a82423`. Branch off the base.
- Build: `bazel build --config=opt //src/mongo/db:mongod` (~8 min, 96 cores). Tests: `resmoke`.
- **Target is master.** Reference 7.0.34 source: `/home/junyao/code/mongo-r7.0.34` (it lacks
  `nextKeyValueView`, `SortedDataKeyValueView`, `storage/key_string/`, `exec/classic/` — the
  covered_fused work cannot compile there). The measured baseline (stock 7.0.34,
  `mongodb://localhost:57017`, db `bench`, `layout2_view`, 10M docs, no auth) cannot run your build
  — A/B on your own binary.
- Workload shapes: `/home/junyao/code/pageindex/ConDB/bench/db/bench_all_ops_layouts.py` — read it.
- A/B runner: `bench/db/condb_ab_campaign.py`. Prior subtree harnesses under
  `bench/db/bench_subtree_*.py`.

## Discipline — five failure modes have recurred in this project

1. **Unit mixing.** Server CPU / client wall / retired instructions are three quantities — the
   covered_fused result above was once misquoted as CPU when it was instructions. Never compare
   across.
2. **Unpaired arms.** Alternate within blocks; per-block paired deltas. An unpaired −14% on this
   very operation became +0.5% paired.
3. **Inclusive/exclusive confusion.** Never add sibling inclusive percentages.
4. **Fabricated ceilings.** Never copy a measured value into a ceiling column.
5. **Non-like-for-like arms.** Verify output equality element-wise, every block, on all 200 cohort
   inputs when you run the cohort.

Plus: **single binary**, env-var gate read once at startup (layout variance 2.6 pp); **control
endpoint** — a small-result query the gate cannot help is a natural one; **activation counter**
printed at exit. **Never benchmark while anything is compiling** — three sibling agents build here;
announce dataset/duration/load first. This operation's run-to-run spread is ~24% at P50 and
input-dependent (12.7% at 96k rows) — report what you observe, claim nothing smaller.
"Cohort-weighted" and "per-subtree median" are different quantities; name which you mean, every
time.

## Acceptance gate (per change you keep)

1. A test that **fails on the unpatched base**, artifact retained.
2. **Non-intrusion proof**, including `internalQueryExecYieldIterations: 1`, and specifically: no
   regression on single-batch/small results, and error-path behaviour stated.
3. **Locally-decidable safety**; the tail run under concurrency if the change reaches a measurable
   state.
4. **Blank-context agent review** to MongoDB query-team PR standards; fix, don't argue.
5. Honest limits: what you did not measure, what would falsify the result.

## Deliverables

- Branch pushed to `origin`, **Draft** PR. **No SERVER- ticket exists — do not invent one; never
  claim upstream-ready.** No AI-authorship traces in commits or PR text.
- Never push the ConDB repo; commit there locally only.
- A written record of every lead tried, with its number — negative results retained.
