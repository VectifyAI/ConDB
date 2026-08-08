# Task C — Cut PyMongo's fixed per-command cost

MongoDB is paying for this work. PyMongo is MongoDB's official driver, so this is a MongoDB-side
change like any other — and on the short reads it is the largest single term.

**This task covers all four operations of the workload**, because the cost is per command and does
not depend on the query. It is the only MongoDB-side work that touches `get_entity`, which already
has the server-side fast path and therefore has nothing else to gain.

## What is wrong today

A hand-written 40-line `OP_MSG` client in the same process, using the same `bson._cbson` codec,
costs **16.9 µs of client CPU per command** where PyMongo's `find_one` costs **79.1**. That 62.2 µs
is driver overhead the wire exchange does not require. It is corroborated at 62.98 µs by a separate
harness and run.

Ablation ladder — five arms peeled one layer at a time, 14 blocks × 500 iterations, rotated within
each block, all verified to return identical documents every block, CPU measured with
`time.process_time()` (`bench/db/runs/pymongo_fused_20260807/ladder.json`, harness
`bench/db/bench_pymongo_ladder.py`):

| arm | wall µs | client CPU µs | CPU removed by this step |
|---|---|---|---|
| `find_one` — public API, builds a `Cursor` | 216.4 | 79.1 | — |
| `Database.command` — byte-identical wire, no `Cursor` | 191.4 | 54.8 | **24.4** |
| `Connection.command` with session and client, connection held | 167.7 | 27.4 | **27.3** |
| `Connection.command` with neither | 163.3 | 24.6 | **2.9** |
| hand-written `OP_MSG` on the same socket | 152.6 | 16.9 | **7.7** |

Two targets, in order of size.

### C1 — Pool-checkout fast path · 27.3 µs, 44% of the driver cost

Server selection, pool checkout and pool checkin run on **every** operation, against an
already-pooled connection and an unchanged topology. Nothing has been attempted here. The `conn_sess`
arm above exists specifically to separate this from session work: session application and
cluster-time gossip are only **2.9 µs**, and a supported fast path cannot drop the implicit session,
so 27.3 µs is the honest ceiling and 30.2 µs is not.

### C2 — Skip `Cursor` construction for single-batch replies · 24.4 µs, 39%

`find_one` and `Database.command` put **byte-identical 304-byte documents on the wire** — captured
with a `CommandListener`, same eight fields including `lsid` — yet `command` is 24.4 µs cheaper in
client CPU. Paired 16 blocks (`bench/db/report/evidence/review_20260807/v2_driver_paired.json`):
−23.46 µs paired median, **16/16 blocks**; wall −12.8%, 15/16; server CPU −1.25 µs, 9/16 — within
spread. So the saving is not the wire, not the server, and not the retry machinery (2.6 µs). It is
`Cursor` construction and teardown for a reply that arrives with `cursor.id == 0` and will never be
iterated.

`find_one` already sets `limit:1`/`singleBatch`, so **the driver knows before it sends** that the
reply cannot need a cursor. `get_children` returns ~11 rows, which always fit under the 101-document
default first batch, so it is in the same situation but the saving there is unmeasured — an 11-row
reply also costs eleven `__next__` iterations the one-row measurement does not include.

## Two candidate explanations that are already dead — do not re-litigate

- **Syscalls are not the cost.** `strace -c` over 1,300 lookups: 1 `sendto`, 2 `recvfrom`, 2 `poll`
  per operation, with `setsockopt` and `fcntl` appearing ~12 times *in total*, not per operation —
  PyMongo's timeout cache (`pymongo/synchronous/pool.py:192-197`) already works. An earlier claim of
  "four `settimeout` syscalls per operation" was simply false. Fusing the header and body reads into
  one speculative read was implemented and measured at **−0.50%, 7/12 blocks, [−8.33, +10.37]** —
  inside noise (`bench/db/runs/pymongo_fused_20260807/get_node_8k.json`, harness
  `bench_pymongo_fused_recv.py`).
- **There is no hot spot.** `cprofile.json` in the same directory: over 3,000 calls `find_one` spends
  1.066 s across **240 distinct functions**, largest non-socket frame 0.026 s
  (`cursor.py:96 __init__`); `Database.command` 0.882 s across 175 functions. These are **wall** times
  inflated by the profiler and must not be quoted as CPU. There is no single frame to delete — the
  cost is the layering, which is why C1 and C2 are structural rather than micro-optimizations.

## Environment

- PyMongo 4.12 at `/home/junyao/code/pageindex/ConDB/.venv/bin/python3` (Python 3.14.5); source under
  `.venv/lib/python3.14/site-packages/pymongo/`. For a real contribution you will want a checkout of
  `mongodb/mongo-python-driver` — clone it yourself; there is no fork of it on this box yet.
- Server: stock **MongoDB 7.0.34** at `mongodb://localhost:57017`, db `bench`, collections
  `layout2_view` (10M docs) and `layout_shared_text` (9M), no auth. Read-only work.
- Workload definitions: `bench/db/bench_all_ops_layouts.py`. Read it; all four operations matter here.
- Prior evidence: `bench/db/report/ops/get_node.md` §3 and §5, and `get_entity.md` §4.
- The box has 96 cores and is shared. **Do not change any server parameter.**

## Measurement discipline — not optional

Five failure modes have recurred in this project.

1. **Unit mixing.** Client CPU, client wall and server CPU are three quantities. The ladder above is
   client CPU; the cProfile numbers are wall. Never compare across them.
2. **Unpaired arms.** Alternate within blocks, report per-block paired deltas. An unpaired −14% here
   became +0.5% under pairing.
3. **Inclusive/exclusive confusion** in profiles.
4. **Fabricated ceilings.**
5. **Non-like-for-like arms.** Verify output equality element-wise, every block. A faster arm that
   returns a different object is the failure this catches.

Specific to this task:

- **`time.process_time()`, not `os.times()`.** The latter reports in 10 ms clock ticks, so over 400
  iterations every arm's CPU snaps to a multiple of 25 µs. This harness was written with `os.times()`
  first and the quantization was visible in the output.
- **Hold one connection fixed across arms where you can.** Fresh connections to the same `mongod`
  differ by 14–26% in P50 on this two-socket box
  (`bench/db/runs/pymongo_fused_20260807/connection_lottery_20.json`). Any wall-time comparison whose
  arms sit on different sockets is confounded; client-CPU comparisons are not.
- Report the run-to-run spread you observe; never claim an effect smaller than it.
- Announce dataset, duration and load before heavy runs.

## Acceptance gate

1. **A test that fails without the change**, artifact retained.
2. **Proof of non-intrusion.** For C1 this is the hard part: the fast path must not skip server
   selection when the topology has actually changed, must not bypass a connection that has failed a
   health check, must not break `maxIdleTimeMS` recycling, and must behave correctly under a replica
   set failover and under `retryReads`. A fast path that is fast because it stopped checking things is
   not a contribution.
3. **Correctness under concurrency.** Both changes touch shared state; run the driver's own test
   suite, not only your benchmark.
4. **A blank-context agent review** to MongoDB driver-team standards. Fix what it finds.
5. Honest limits, including which of the four operations you measured and which you assumed.

## Deliverables and constraints

- A branch and a **Draft** PR against your own fork of the Python driver.
- **There is no real PYTHON- or SERVER- ticket. Do not invent one. Do not claim upstream-ready.**
- No AI-authorship traces in commits or PR text.
- Never push the ConDB repo — commit there locally only.
- Say plainly, with `file:line`, anything you could not do.

## What would make this not worth shipping

For C1: if the checks being skipped turn out to be load-bearing under failover or pool recycling, the
honest outcome is a smaller fast path than 27.3 µs, and you should say what the safe subset is worth
rather than shipping the unsafe version. For C2: if returning a document without constructing a
`Cursor` changes any observable — command monitoring events, `explain` behaviour, exception types,
read-preference handling — the saving is not free and the report must price it. In both cases, a
measured "the safe version is worth N, not 27" is a good result.
