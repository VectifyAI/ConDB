# `get_subtree` — retrieve an entire subtree

**What MongoDB should change to serve this shape faster.** The workload is a JSON tree; this
operation reads a whole subtree, the shape that distinguishes a tree store from a key-value store.
Everything below is scoped to changes in `mongod` or in PyMongo. Application-side workarounds appear
only in §6, as evidence bounding what a server change is worth.

**MongoDB 18.1 / 208.1 ms against PostgreSQL 13.6 / 164.9 ms (P50 / P95), 1.33×** — the narrowest
headline ratio of the four, and the only gap measured in milliseconds.

> **Two findings supersede the original recommendation; §9 lists every correction.** The change this
> document asked for — mongod transmitting before a batch is full — cannot be built, for reasons
> that are protocol-level rather than effort-level. The overlap it was meant to deliver is already
> available through an exhaust cursor, **including behind `mongos`**, which this document previously
> and wrongly said was impossible. The server change that *was* built and measured is M1b:
> −13.1% server CPU, −6.4% client wall at the cohort P50.

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

*The measurements in this section stand. The conclusion originally drawn from them — that `mongod`
should transmit before the batch is full — does not; see M1 and §9.*

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

### M1 — Let sharded deployments use the exhaust cursor they already have · **no server work needed**

*Replaces an earlier recommendation that mongod be changed to transmit before a batch is full. That
recommendation was wrong twice over; both errors are recorded in §9.*

The overlap an exhaust cursor buys — bounded at about 40% of wall in §6 — was treated here as
unreachable for sharded deployments because exhaust "is refused behind `mongos`". **It is not.
`mongos` implements exhaust in full**, and the refusal is imposed by the driver before anything
reaches the wire:

- `src/mongo/s/commands/strategy.cpp:488` — `opCtx->setExhaust(OpMsg::isFlagSet(m,
  OpMsg::kExhaustSupported))`
- `src/mongo/s/commands/query_cmd/cluster_getmore_cmd.h:111-115` — `if (opCtx->isExhaust() &&
  response.getCursorId() != 0) reply->setNextInvocation(boost::none);`
- `src/mongo/s/commands/strategy.cpp:1332-1337` — propagates `shouldRunAgainForExhaust` into the
  `DbResponse`

against PyMongo 4.12.0:

```python
if self._cursor_type == CursorType.EXHAUST:
    if self._collection.database.client.is_mongos:
        raise InvalidOperation("Exhaust cursors are not supported by mongos")
```

Every measurement in this project went through PyMongo, and no experiment run through PyMongo can
distinguish "mongos cannot do this" from "the driver will not ask". That is why the claim survived.

**Verified on the wire.** `bench/db/exhaust_through_mongos.py` speaks OP_MSG over a raw socket, sets
`exhaustAllowed` itself, and counts replies to a *single* `getMore` by decoding `flagBits`; nothing
is inferred from timing. Cluster from `bench/db/setup_sharded_for_exhaust.sh`.

| endpoint | replies to one `getMore` | unsolicited | rows |
|---|---|---|---|
| standalone `mongod` (instrument check) | 11 | 10 | 11,686 |
| **`mongos`, one shard** | **20** | **19** | 20,000 |
| **`mongos`, two shards, merged** | **20** | **19** | 20,000 |

It streams through a router, and keeps streaming when `mongos` merges two shards — the case most
likely to have broken.

**What it is worth.** Same client, same socket, same batch size; only the `exhaustAllowed` flag
differs. Paired, arms alternating within blocks:

| cluster | paired delta | blocks faster | median |
|---|---|---|---|
| one shard | **−23.41%** [−26.65, −8.91] | 8/8 | 20.2 ms vs 26.0 ms |
| two shards | −19.40% [−56.15, +7.86] | 6/8 | 39.3 ms vs 52.0 ms |

The single-shard figure is the trustworthy one; the two-shard run is directionally the same but far
noisier and is corroboration of direction only.

**The ask is not a mongod change.** It is a question for whoever owns the driver restriction: what
is it protecting against, given the server streams correctly through a two-shard merge? An exhaust
cursor monopolises its connection for the cursor's lifetime, which has pooling consequences behind a
router fronting many shards, and interaction with retryable reads, load-balancer mode and failover
is untested here. Any of those may be a good reason. **"Exhaust cursors are not supported by
mongos" is not one, because the server supports them.**

Scope: one `mongos`, two single-node shards, no auth, no load balancer, no failover, a synthetic
collection, and a client that is not a production driver. Sized to answer a protocol question, not
to produce a throughput number.

### M1b — Fold the covered projection into the index scan · `mongod` · **implemented and measured**

The one server change from this work that was built and kept. A `PROJECTION_COVERED` directly above
an `IXSCAN` reads a key the scan has just materialised in full and copies it again: the storage
cursor decodes every component into a `BSONObj` with placeholder field names, the projection stage
walks it and builds a second object with real names, and components the projection does not want
were decoded for nothing.

When nothing else consumes the materialised key, the scan decodes straight into the projected object
via `SortedDataKeyValueView` — the zero-copy API SBE and express already use and classic `IndexScan`
did not. Excluded components are still decoded, into a reused scratch buffer, because
`key_string::TypeBits::Reader` is positional and skipping one desynchronises the rest.

Measured on 10M documents, paired per block, arms alternating in one process against one dbpath:

| rows | retired instructions | server CPU | client wall |
|---|---|---|---|
| 11,686 | −13.80% | −13.12% | −6.36% |
| 97,773 | −14.58% | −11.95% | −5.18% |
| 1,404,566 | −14.47% | −13.04% | −5.23% |

Every block improved on every measurement; no output differences. Shapes that cannot fuse cost
+0.06% and +0.02% of retired instructions. **Wall improves by less than CPU because the server is
only ~55% of this operation** — transmission and client-side BSON decode are untouched. Off by
default behind `internalQueryEnableFusedCoveredProjection`.

### M2 — Non-key payload columns in indexes · `mongod` · attacks 36.6% of server CPU · **feature, not a patch**

This is the structural difference in §3, and for a tree workload it is the one that recurs: reading a
subtree means returning stored text for thousands of nodes, and MongoDB has no way to store that text
in an index without order-encoding it into the key. PostgreSQL's `INCLUDE` does exactly this. The
term it would attack is 36.6% of this operation's server CPU.

**No implementation, no measurement, no bound beyond that 36.6%.** Recorded because it is the answer
to "why is MongoDB's covered scan more expensive than PostgreSQL's" and because a covering index that
stored payload uninterpreted would also make M3 unnecessary. It is a storage-format change, which is
a different class of work from the rest of this list.

### M3 — Fuse the covered-projection decode · **superseded by M1b**

*Kept as the record of what was known before the work was done. M1b is this idea built against
the real operation; it measures −13.1% of server CPU at the cohort P50, not the −4 to −6% a
microbenchmark suggested, because the implemented version also removes the intermediate key
object rather than only the second copy.*

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

**Why this looked unusable — and the part of that which is wrong.** PyMongo refuses exhaust behind
`mongos` (`pymongo/synchronous/cursor.py`), refuses it with automatic encryption (`:1099`), and will
not combine it with `limit()` (`:456`). The encryption and `limit()` restrictions stand. **The
`mongos` restriction is the driver's, not the server's**: `mongos` implements exhaust and streams
correctly through a two-shard merge, verified on the wire in M1. The earlier text here — that this
"excludes every sharded deployment", and that the exclusion is the argument for a server-side
streaming change — was wrong on both counts.

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

Three things changed in the course of acting on that.

**The largest item needs no server work.** The overlap that closes most of this gap is an exhaust
cursor, and this report previously held that sharded deployments could not have one. `mongos`
implements exhaust in full and streams correctly through a two-shard merge — verified on the wire,
19 unsolicited replies to a single `getMore`, worth −23.4% on the shape tested. What blocks it is a
client-side check in PyMongo whose error message is not true of the server it names. **M1 is now a
question for the drivers team, not a feature request for the query team.**

**The server-side streaming change that was recommended cannot be built.** A client that did not ask
for exhaust reads exactly one reply per request, so emitting several is not available to `mongod`
unilaterally; and a single reply cannot be streamed either, because its length is patched into byte
0 after the body is complete. That was worth measuring before abandoning: transmission of the P50
reply costs ~2,800 µs, about 20% of wall. It is unreachable from the reply path. See §9.

**What was built instead is worth roughly a third of what M1 would be.** M1b folds the covered
projection into the index scan: **−13.1% server CPU, −6.4% client wall** at the cohort P50, flat
across a 120× range of input sizes, no output differences. It is honest to state the wall figure as
single-digit: the server is only ~55% of this operation after the change, and transmission (~21%)
and client-side BSON decode (~23%) are untouched and out of reach from `mongod`.

Underneath sits **M2**, the absence of non-key payload columns in indexes. A ceiling probe puts the
whole cost of carrying the payload through the key at 37% of server CPU, but that is a loose upper
bound — `INCLUDE` would keep the payload in the index and still copy it, and the narrow-index arm
walks a 0.28 GB B-tree against 4.66 GB. What `INCLUDE` actually removes is the escape scan and
TypeBits for payload components, a few percent of server CPU. **Not recommended for this workload.**

---

## 9. Corrections to earlier versions of this document

Recorded because each was acted on, and because the reasoning that produced them is the part worth
not repeating.

**"Exhaust is refused behind `mongos`, excluding every sharded deployment."** Wrong. That is
PyMongo's refusal, not the server's. It survived because every measurement in this project ran
through PyMongo, and no experiment run through PyMongo can distinguish server incapability from
driver refusal. Settled by speaking OP_MSG directly. See M1.

**"A server that began transmitting before the batch filled would deliver the same overlap to every
driver."** Not reachable. `makeExhaustMessage` (`transport/session_workflow.cpp:299`) gates
`kMoreToCome` on `kExhaustSupported`, a flag the *client* sets; a second reply to a client that did
not set it desynchronises the connection. And a single OP_MSG reply cannot be streamed, because
`Message` is one buffer whose header length is written by `OpMsgBuilder::finish()` after the body is
complete (`rpc/message.h:327`, `rpc/op_msg.cpp:432`). Both were established by reading the code and
then measuring what the change would have been worth — ~20% of wall — rather than by assertion.

**"`batch_size` alone is −11.2% at 96,238 rows."** Does not reproduce. Re-measured server-side as a
batch byte cap: retired instructions move under 1% for every batch size tried, while server CPU
worsens 4–8.6% from the extra round trips. The first re-measurement produced a false −5.20% that was
an artifact of the probe having no per-arm settle, which is itself worth recording — the block
sequence ran −35%, −28%, … , −1.6% as the cache warmed.

**"`KeyString::toBson` 24.4% + `ProjectionStageCovered::transform` 12.3% = 36.6% of server CPU."**
Reproduces on master (21.46% + 13.90% = 35.36%) but is an *inclusive* figure, and was being read as
though it were removable. Self time for the two is 3.14% and 3.96%; the irreducible read underneath
them is 7.22% inclusive. The implemented fusion removes ~13% of server CPU, not ~36%.

**"The `shouldDedup` guard is not reachable through a covered projection."** Wrong, and recorded in
the implementation's lead log rather than here: an index that is multikey overall can still fully
provide a field whose own path is not multikey, so a covered projection over a deduplicating scan is
reachable, and fusing it would return one row per index entry instead of one per document.
