# Optimize MongoDB for `get_entity` — fetch one entity by `_id`

You own this operation end to end. MongoDB is paying for this work; every deliverable is a change to
MongoDB's own code — here that means **PyMongo first**, because the server side of this operation is
already on MongoDB's fastest plan. **This document gives you an initial plan. Execute it first. If
its measured effect is insufficient, do not stop — continue down the fallback leads, and past them
if necessary, under the same discipline.**

Three sibling agents work the other operations on this box. Your lane is the **driver** (PyMongo)
and, in the fallback, the **server command path**. Express eligibility belongs to the `get_node`
agent, the plan cache to `get_children`, cursor batching to `get_subtree` — do not modify those
areas. Your driver changes benefit all four operations; that is fine, it is still your lane.

## The operation and the gap

Unhinted `find_one` on `_id` in `layout_shared_text` (9,000,301 docs), served by
`PROJECTION_SIMPLE → IDHACK` (verified live). **MongoDB 0.142 ms vs PostgreSQL 0.074 ms P50, 1.9×.**
Server CPU 45.4 µs vs 19.5 (prepared) / 30.0 (unprepared).

This operation is special: it already has the server fast path, so **plan caching moves it −0.05%
and a compound-key fast path does not apply**. Whatever remains is the per-command floor — which is
why its gap is the ceiling on what the other agents' server work can achieve, and why your lane is
the driver. From the retained inclusive profile (`perf_nolog/get_entity_hit.perf.data`, 83,543
samples): the entire point lookup (`IDHackStage::doWork`) is **3.48 µs**; sending the reply
(`sinkMessage`) is **10.09 µs**. The lookup is not the cost. Full analysis:
`/home/junyao/code/pageindex/ConDB/bench/db/report/ops/get_entity.md`. Read it.

## Initial plan: cut PyMongo's fixed per-command cost

A hand-written 40-line OP_MSG client in the same process, same `bson._cbson` codec, costs
**16.9 µs of client CPU per command**; PyMongo's `find_one` costs **79.1**. The 62.2 µs difference
is driver overhead the wire exchange does not require, corroborated at 62.98 by a second harness.
Ablation ladder, 14 blocks × 500, arms rotated, outputs verified identical every block
(`bench/db/runs/pymongo_fused_20260807/ladder.json`, harness `bench/db/bench_pymongo_ladder.py`):

| arm | client CPU µs | removed by this step |
|---|---|---|
| `find_one` (builds a `Cursor`) | 79.1 | — |
| `Database.command` (byte-identical wire) | 54.8 | **24.4** |
| `Connection.command` + session/client, connection held | 27.4 | **27.3** |
| `Connection.command`, neither | 24.6 | 2.9 |
| hand-written OP_MSG | 16.9 | 7.7 |

**C1 — pool-checkout fast path, 27.3 µs (44%).** Server selection, pool checkout and checkin run on
every operation against an already-pooled connection and an unchanged topology. Session application
and cluster-time gossip are only 2.9 µs and a supported fast path cannot drop the implicit session —
so 27.3 is the honest ceiling, not 30.2. Nothing has been attempted here.

**C2 — skip `Cursor` construction for single-batch replies, 24.4 µs (39%).** `find_one` and
`Database.command` put **byte-identical 304-byte documents on the wire** (captured with a
`CommandListener`, including `lsid`) yet differ by 24.4 µs of client CPU; server CPU difference is
−1.25 µs, within spread. `find_one` sets `limit:1`/`singleBatch`, so the driver knows *before
sending* that the reply cannot need a cursor. The saving is `Cursor` construction/teardown for a
reply that arrives with `cursor.id == 0`.

Two dead ends — do not re-litigate. **Syscalls**: 1 sendto + 2 recvfrom + 2 poll per op, zero
per-op `setsockopt` (the timeout cache at `pymongo/synchronous/pool.py:192-197` works); fusing the
header/body reads measured −0.50%, inside noise (`get_node_8k.json`). **Hot spots**: none —
`cprofile.json` shows 240 distinct functions with the largest non-socket frame at 0.026 s of
1.066 s wall. The cost is the layering, which is why C1/C2 are structural.

## If the effect is insufficient, continue — in this order

The bar: no single-digit percentages of the operation. The driver term is 60.3 µs on this
operation's own shape (measured; reply is 1,160 B vs `get_node`'s 613), so C1+C2 clear the bar if
they survive their correctness constraints.

1. **Produce the exclusive phase partition of the server's 45.4 µs.** The perf data is retained
   (`get_entity_hit.perf.data`, 194 MB); only the re-attribution pass (the
   `decomp_get_node/get_node_phases.txt` equivalent — see `phases.py` there) was never run. No
   re-measurement needed. This turns the inclusive profile into a target list.
2. **Attack the largest term it reveals in the server command path** — candidates already visible
   inclusively: reply send 10.09 µs, request receive 4.11, collection acquisition 3.62+0.93, parse
   1.99, `getExecutorFind` residue 2.17. This is `mongod` work on master; single binary, env-var
   gate, control endpoint, activation counter, as below. Note 8.39% of self samples are
   EDR/netfilter kernel modules — environment, not MongoDB; separate them before claiming.
3. If you find a better direction, take it — same discipline, same bar.

## Environment

- PyMongo 4.12 under `/home/junyao/code/pageindex/ConDB/.venv/lib/python3.14/site-packages/pymongo/`
  (Python 3.14.5). For the contribution, clone `mongodb/mongo-python-driver` yourself — no fork of
  it exists on this box yet.
- Server: stock 7.0.34 at `mongodb://localhost:57017`, db `bench`, `layout_shared_text` (9M) and
  `layout2_view` (10M), no auth. **Do not change any server parameter.**
- For fallback server work: fork `/home/junyao/code/mongo` (`origin` =
  `git@github.com:carsontung666/mongo.git`), pinned base
  `0561c098b99ac5e929005e70a2e37d7a97a82423`, `bazel build --config=opt //src/mongo/db:mongod`,
  target master; 7.0.34 reference source at `/home/junyao/code/mongo-r7.0.34`.
- Workload shapes: `/home/junyao/code/pageindex/ConDB/bench/db/bench_all_ops_layouts.py` — read it.

## Discipline — five failure modes have recurred in this project

1. **Unit mixing.** Client CPU / client wall / server CPU are three quantities. The ladder is client
   CPU; cProfile output is wall. Never compare across.
2. **Unpaired arms.** Alternate within blocks; per-block paired deltas. An unpaired −14% became
   +0.5% paired.
3. **Inclusive/exclusive confusion.** §2's profile numbers are inclusive — never add siblings.
4. **Fabricated ceilings.**
5. **Non-like-for-like arms.** Verify output equality element-wise, every block — a faster arm
   returning a different object is the failure this catches.

Plus: **`time.process_time()`, never `os.times()`** (10 ms ticks quantize everything to 25 µs —
this exact mistake happened in this harness's first version). **Hold one connection fixed across
arms** — fresh connections differ 14–26% in P50 on this two-socket box
(`connection_lottery_20.json`); wall comparisons across sockets are confounded, client-CPU ones are
not. **Never benchmark while anything is compiling** — three sibling agents build mongod on this
box; announce dataset/duration/load first. Report observed spread; claim nothing smaller.

## Acceptance gate (per change you keep)

1. A test that **fails without the change**, artifact retained.
2. **Non-intrusion proof.** For C1 this is the hard part: the fast path must not skip server
   selection when the topology actually changed, must not reuse a connection that failed a health
   check, must not break `maxIdleTimeMS` recycling, must behave under replica-set failover and
   `retryReads`. Fast because it stopped checking is not a contribution.
3. **Correctness under concurrency** — run the driver's own test suite, not only your benchmark.
4. **Blank-context agent review** to MongoDB driver-team standards; fix, don't argue.
5. Honest limits: which operations you measured, which you assumed; for C2, any observable that
   changes (monitoring events, exception types, read-preference handling) priced explicitly.

## Deliverables

- Branch + **Draft** PR against your own fork of the Python driver (and of mongo, if the fallback
  produces server work). **No PYTHON-/SERVER- ticket exists — do not invent one; never claim
  upstream-ready.** No AI-authorship traces in commits or PR text.
- Never push the ConDB repo; commit there locally only.
- A written record of every lead tried, with its number — negative results retained.
