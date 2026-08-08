# `get_subtree` — retrieve an entire subtree

**What MongoDB should change to serve this shape faster.** The workload is a JSON tree; this
operation reads a whole subtree, the shape that distinguishes a tree store from a key-value store.
Everything below is scoped to changes in `mongod` or in PyMongo. Application-side workarounds appear
only in §6, as evidence bounding what a server change is worth.

**MongoDB 18.1 / 208.1 ms against PostgreSQL 13.6 / 164.9 ms (P50 / P95), 1.33×** — the narrowest
headline ratio of the four, and the only gap measured in milliseconds.

The query is a hinted root lookup followed by a hinted range scan over
`layout2_rootcause_exact_cover`, an exactly-covering index on `(path, node_id, title, summary)`,
projecting `{node_id, title, summary}` sorted `(path, node_id)`. It runs `PROJECTION_COVERED` over
`IXSCAN` with **no FETCH** (`totalDocsExamined: 0`, verified live) and no `SORT` stage. PostgreSQL
runs an `Index Only Scan using sql_native_path_view_cover` with `INCLUDE (title, summary)` and
`Heap Fetches: 0`, also verified live.

Cohort of 200 subtrees: rows min 5,006 / median 11,671 / P90 96,238 / max 1,404,566. **The 18 inputs
above 100k rows carry 68.9% of MongoDB's cohort time and 69.6% of PostgreSQL's** — the tail is where
the work is, not a MongoDB-specific concentration. (They are 66.1% of the *rows*.)

Provenance. Server figures are CPU, client figures are wall, never subtracted from one another. The
decomposition arms come from `mongo_cpu_arms_nolog.json`, a run with `slowms` raised to 100; the
server's configured state is `slowms=0`, where the same arm reads 14,860 / 8,826 µs. The PostgreSQL
arm is prepared, which barely matters here — §7 shows planning is ~157 µs of 14.1 ms (unprepared:
9,969 / 7,250).

---

## 1. Where the gap is — and it is not where the other three are

| at the cohort P50 input (11,686 rows) | MongoDB | PostgreSQL | gap | share |
|---|---|---|---|---|
| client wall | 14,127 µs | 9,858 | 4,269 | 100% |
| server CPU | 8,330 | 7,050 | 1,280 | **30%** |
| residual (wall − server CPU) | 5,797 | 2,808 | 2,989 | **70%** |

MongoDB's server does **18% more CPU work** than PostgreSQL's, but the operation takes **43% more
wall time**. The difference is not work. It is that MongoDB's server and client never run at the same
time.

## 2. The mechanism: batch assembly serializes the two sides

`mongod` assembles a `find`/`getMore` batch **to completion** before any of it reaches the transport.
`src/mongo/db/query/find_common.cpp:63` sets `kMaxBytesToReturnToClientAtOnce` to
`BSONObjMaxUserSize` (16 MB); the fill loops are `find_cmd.cpp:653` and `getmore_cmd.cpp:388`; the
reply is handed to the transport only after the command returns. PostgreSQL emits each row as a
`DataRow` as it is produced.

At the cohort P50 the whole 11,686-row / 5.11 MB result arrives in **two round trips**. There is
essentially nothing to overlap.

Measured at the socket rather than by CPU accounting. Round-trip counts are
`serverStatus.metrics.commands` deltas. Artifacts: `review_20260807_subtree/wire_trace_out.json`
(11,686), `wt_p90.json` (96,238), `wt_tail_base.json` / `wt_tail_exh.json` (1,404,566).

| rows | arm | wall ms | first-byte wait | share of wall | `find`+`getMore` |
|---|---|---|---|---|---|
| 11,686 | baseline | 20.19 | 9.08 | **45.0%** | 1 + 1 |
| 11,686 | pipelined | 10.79 | 2.91 | 26.9% | 1 + 11 |
| 96,238 | baseline | 142.11 | 61.07 | **43.0%** | 1 + 3 |
| 96,238 | pipelined | 94.09 | 6.99 | 7.4% | 1 + 96 |
| 1,404,566 | baseline | 2127.09 | 849.22 | **39.9%** | 1 + 37 |
| 1,404,566 | pipelined | 1128.27 | 18.00 | 1.6% | 1 + 1404 |

**The baseline spends 40–45% of its wall time blocked before the first byte of a reply arrives.**

Two qualifications. The instrument (`wire_trace.py:173-197`) sums the blocking time of the first
`recv_into` of each **gap-split receive group** — boundary being a `sendall` or a pause over 0.2 ms —
not of each reply, so the runs have 3 / 7 / 75 groups against 2 / 4 / 38 round trips; restricting to
groups following a `sendall` moves the 96,238-row share 41.4% → 40.8%, so the figures survive. And
the block is not purely server time: the URI is the published Docker port, so it contains the
container relay, ~20–25 µs per round trip — a rounding error at 2 round trips, but not zero.

## 3. Where MongoDB's server CPU goes

One instrument, one unit: a perf profile of 8,156.5 µs of server CPU
(`BOTTLENECK_20260806.md:404`, table at `:412-418`).

| | share of server CPU |
|---|---|
| `KeyString::toBson` | 24.4% |
| `ProjectionStageCovered::transform` | 12.3% |
| **key materialization + covered projection** | **36.6%** |
| `__wt_btcur_next` — the index walk itself | 7.9% |

MongoDB has no non-key payload columns: every covering field is order-encoded into the KeyString and
must be decoded sequentially, because skipping a component desynchronizes `TypeBits::Reader`.
PostgreSQL's `INCLUDE (title, summary)` stores those columns uninterpreted and returns them directly.

**One asymmetry runs against MongoDB and is not a MongoDB property.** `BOTTLENECK_20260806.md:450-458`
records that MongoDB's range scan does not filter `tree_id` while PostgreSQL's does, so MongoDB
decodes four key components where PostgreSQL reads three key columns plus two payload columns. Part
of the 36.6% is that extra component, not the encoding scheme.

It is **not** an index-size effect. On disk, `layout2_rootcause_exact_cover` is 4,662,206,464 B
against PostgreSQL's `sql_native_path_view_cover` at 5,506,342,912 B, both verified live: **MongoDB's
index is 15.3% smaller**. Whether bytes-on-disk refutes a *scan-cost* hypothesis is a weaker claim and
is not established either way.

---

## 4. What MongoDB should change

### M1 — Transmit before the batch is full · `mongod` · ~40% of wall · **the largest single item in this project**

The machinery already exists: `src/mongo/transport/session_workflow.cpp:292`, `makeExhaustMessage`,
sets `kMoreToCome` (at `:328`) and makes the server produce without waiting for a `getMore`. What is
missing is that it only happens when the client asks for an exhaust cursor, and exhaust is
unavailable behind `mongos`. **A server that began transmitting before the batch filled would deliver
the same overlap to every driver and to sharded deployments**, with none of §6's restrictions.

Bound, from the driver-side proxy in §6, measured **against MongoDB's own baseline** (§6's table uses
the same phrase for a comparison against PostgreSQL — they are different quantities): **−40.5%
cohort-weighted** over 200 subtrees (pass 2: −41.7%), **−28.2% at the per-subtree median**, and −40
to −46% on individual large reads.

The bound is imperfect in one respect: the proxy moves two levers, and §6 item 1 shows exhaust alone
is +2.0% at the median. A streaming server would plausibly make batch size moot, but nothing measured
here establishes that.

Target version: master; cannot be prototyped on 7.0.34 without back-porting.

### M2 — Non-key payload columns in indexes · `mongod` · attacks 36.6% of server CPU · **feature, not a patch**

This is the structural difference in §3, and for a tree workload it is the one that recurs: reading a
subtree means returning stored text for thousands of nodes, and MongoDB has no way to store that text
in an index without order-encoding it into the key. PostgreSQL's `INCLUDE` does exactly this. The
term it would attack is 36.6% of this operation's server CPU.

**No implementation, no measurement, no bound beyond that 36.6%.** Recorded because it is the answer
to "why is MongoDB's covered scan more expensive than PostgreSQL's" and because a covering index that
stored payload uninterpreted would also make M3 unnecessary. It is a storage-format change, which is
a different class of work from the rest of this list.

### M3 — Fuse the covered-projection decode · `mongod` · −4 to −6% CPU on a microbenchmark

`mongodb_recheck_20260806/covered_fused/` implements `key_string::toBsonProjectedSafe()`, decoding
components in order while emitting only the wanted ones — the intermediate BSON object the current
path builds and immediately discards. Stated precisely:

- **−8.3% is retired instructions**, 16/16 blocks. The same blocks give a CPU-time ratio of 0.9431,
  i.e. **−4% to −6% in CPU time**.
- Measured on `PointQueryBenchmark/UniqueFieldRangeScanCovered/{1000,10000,100000}/256`, a
  Google-Benchmark binary **on master** — not on `get_subtree`, not on `layout2_view`, not on 7.0.34,
  which lacks `nextKeyValueView`, `SortedDataKeyValueView`, `storage/key_string/` and `exec/classic/`.
- Activation counters (`covered_fused/counters.txt`): the gated path fires 1,487,730–1,579,337 times
  per block, **25,082,542 over 16 blocks**, 0 in every base block.
- **[unretained]** No measurement connects the microbenchmark's −4 to −6% to a share of
  `get_subtree`'s wall time.

Below this project's 10% bar, and listed only because §3 identifies the term it attacks as 36.6% of
server CPU — sequential *traversal* is required, sequential *materialization* is not.

---

## 5. What is not the problem

Worth stating explicitly, because a tree workload invites the wrong guesses:

- **Not planning.** The `getMore` is 95.2% of server CPU and does zero planning; whole-operation
  planning is ~157 µs of 14.1 ms.
- **Not sorting.** There is no `SORT` stage; the index provides the order.
- **Not index size.** MongoDB's covering index is 15.3% *smaller* than PostgreSQL's equivalent.
- **Not document fetch.** `totalDocsExamined: 0` — the scan is fully covered.
- **Not the index walk.** `__wt_btcur_next` is 7.9% of server CPU.

---

## 6. The driver-side proxy for M1, and why it is an instrument rather than a recommendation

`cursor_type=CursorType.EXHAUST` **together with** `.batch_size(1000)`. Full 200-subtree cohort, one
interpreter, one PostgreSQL session, interleaved with rotated arm order, one fixed batch size for
every input, element-wise fingerprint check on all 200 inputs, two independent passes:

| pass | arm | cohort total | P50 | P95 | cohort-weighted **vs PostgreSQL** | mismatches |
|---|---|---|---|---|---|---|
| 1 | baseline | 10.779 s | 15.60 ms | 196.05 ms | +48.9% | 0 |
| 1 | tuned | 6.418 s | 11.35 ms | 111.29 ms | **−11.4%** | 0 |
| 2 | baseline | 10.693 s | 15.73 ms | 191.77 ms | +48.3% | 0 |
| 2 | tuned | 6.231 s | 10.80 ms | 106.05 ms | **−13.6%** | 0 |

**The reversal is tail-carried.** Dropping the largest subtree moves −11.4% → −7.2%; the largest
three → **−0.9%**; the 18 inputs above 100k rows → **+8.0%**. By band the tuned arm is +14.0% against
PostgreSQL below 10k rows, +10.1% from 10–20k, −13.5% from 50–100k, −14.3% above 100k. **The typical
request remains 8–10% slower than PostgreSQL.**

**Why a MongoDB user cannot be told to do this.** Exhaust cursors are refused behind `mongos`
(`pymongo/synchronous/cursor.py:253`, `:398`) and incompatible with automatic encryption (`:1099`) —
excluding every sharded deployment and every cluster fronted by a router. They cannot be combined
with `limit()` (`:456`). **That exclusion is the argument for M1**: the capability exists in the
server and is reachable only through a client option most deployments cannot use.

On abandonment the retained evidence is definite in both directions. `mongo_client.py:2087-2092`
closes the socket when a partly consumed exhaust cursor is abandoned; but `exhaust_semantics.json`
records `client_conn_closed: []` for both abandonment arms while the harness listens for
`ConnectionClosedEvent` (`exhaust_semantics.py:34`), which `Connection.close_conn` publishes
(`pool.py:550-558`) — positive evidence the branch did not execute. A cost *is* retained: 20
abandonments cost **40.2 ms against 30.5 ms**, **+31.8%**. Killing such a cursor server-side is not
clean: the client receives further rows before raising `CursorNotFound`. A retained `pinning` result
reports a concurrent `ping` failing after 2,000 ms at `maxPoolSize: 1`; **[unretained]** the harness
in the tree connects with `maxPoolSize=4` and never writes that key.

Three scope facts:

1. **"Neither lever works alone" is P50-only, and this cuts both ways.** Exhaust alone is +2.0% at
   11,686 rows, −30.3% at 96,238, −45.0% at 1,404,566 — at the tail it captures essentially all of
   the effect (best tuned arm there: −45.8%). **`batch_size` alone is likewise not uniformly inert**:
   +0.5% at 96,238 with `bs8000`, but −3.3% (12/12 blocks) at 11,686 with `bs1000` and −11.2% (10/10)
   at 96,238 with `bs2000`.
2. **`batch_size` is dangerous on the low side**: −27.8% at 250, −35.4% at 1000, −18.9% at 4000, but
   +20.9% at 50 and **+293.7% at 10**. Usable window roughly 250–4000; 100 measures −14.3% with an
   18.4% block spread.
3. **Latency, not capacity — but the capacity picture is a saturation curve, not a flat zero.**
   Per-operation server CPU rises 4.6–9.3% single-client. Throughput gain by client count: **+42.0%
   at 1, +36.9% at 8, +5.0% at 32**, replicated at 32 giving +5.6%. At 32 clients aggregate server CPU
   rises 22.7 → 30.2 cores, +33% — a *rate*, per operation about +26%, not comparable to the
   4.6–9.3% per-operation figure. **Both runs set `row_window: [5000, 30000]`, so no input above 30k
   rows was ever run concurrently** — the tail where the benefit lives is untested under load.

**A stored bucket layout** (256-row documents) gives 1.91×, and 2.16× stacked with the proxy, at
1.45 GB on disk against the 4.66 GB covering index — 31% of the index, plus write-path maintenance.
It is an application-side restructuring, listed to record that materializing the rows in advance
avoids the decode M2 and M3 attack.

---

## 7. Ruled out, with numbers

| | measured |
|---|---|
| `RawBSONDocument` | **2.41× slower** (375,379 vs 155,844 µs at 96,238 rows, distributions disjoint) — the projection returns three fields and the workload reads all three, so a raw document re-scans its bytes per access |
| wire compression, snappy / zstd | +57.7% / +84.1%, 40 subtrees averaging 59,052 rows, 12 blocks; compressors verified engaged by before/after `serverStatus.network.compression` deltas |
| wire compression, zlib | median **+1254.9%** over 8 blocks on one 11,686-row subtree; engagement verified in `battery.json`, not in `wire.json` whose counters are lifetime totals. The payload compresses 3.4–4.8×, so this is loopback-specific and would not transfer to a bandwidth-constrained link |
| range-sharded parallel cursors | −48.3% against **−46.4% for exhaust + `batchSize` 2000** (not exhaust alone, −30.3% at that input), at 1.5× server CPU |
| manual `find`/`getMore` commands | median −3.59%, 11/12 blocks, block min/max [−10.62, +4.71] |
| aggregation reshaping | every variant slower than `find` **[unretained — arms exist in `bench_subtree_wins.py`, no result file was written]** |

**[unretained]** "~24% run-to-run spread" is quoted across this project and stated by no artifact. It
reproduces at 24.6% over 9 live blocks at the P50 input but 12.7% over 5 blocks at 96,238 rows, so it
is input-dependent.

---

## 8. Summary

`get_subtree` is the one operation where MongoDB's server is nearly competitive — 18% more CPU than
PostgreSQL — and loses on wall time anyway, by 43%, because it fills an entire batch before
transmitting while PostgreSQL streams row by row. **40–45% of the baseline's wall time is the client
blocked waiting for a first byte.**

**M1 — transmit before the batch is full — is the largest single change identified anywhere in this
project**, bounded at about 40% of wall, and the machinery for it already exists in the exhaust path;
what is missing is that it requires a client option most production deployments cannot use.
Underneath sits **M2**, the absence of non-key payload columns in indexes, which is 36.6% of this
operation's server CPU and the reason a covered tree scan costs more in MongoDB than in PostgreSQL —
a storage-format question rather than a patch. **M3** attacks a slice of the same term for −4 to −6%
of CPU on a microbenchmark that was never run against this operation.
