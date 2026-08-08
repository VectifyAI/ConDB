# `get_entity` — the driver lane

Record of the PyMongo work on `get_entity` (unhinted `find_one` on `_id` in `layout_shared_text`,
9,000,301 documents, `PROJECTION_SIMPLE → IDHACK`). Companion to `get_entity.md`, which states the
gap; this file states what was tried against it and what each attempt was worth.

Every lead is listed, including the ones that did not pay.

---

## 0. The instrument, and why it changed

Client CPU (`time.process_time()`) is the instrument the earlier ablation used. It is correct on a
quiet box and unusable on this one: three sibling agents build `mongod` here, and at load average
216 the same `find_one` measured 79 µs one minute and 318 µs the next. Nothing about the driver
changed; memory-bandwidth contention changed how many cycles the same work costs.

So the driver work here is attributed in **retired user-space instructions**, read per thread from
`perf_event_open` (`bench/db/perfcount.py`, `PERF_COUNT_HW_INSTRUCTIONS`, `exclude_kernel`). Under
the same load average 216 that made CPU swing 3×, the instruction count held to better than 0.1%
block to block. Instructions say nothing about cache misses or syscall latency, so CPU and wall are
still reported — taken separately, in the window when the box was quiet — and the three are never
mixed or converted into one another.

Two independent checks that the instrument is sound:

- The stock `find_one` measures **334,152 instructions and 79.0 µs of client CPU** on a quiet box.
  The 4.12-era ablation, a different harness on a different shape, put `find_one` at **79.1 µs**.
- The driver term (`find_one` − hand-written OP_MSG) measures **61.4 µs**; `get_entity.md` §4 M2
  quotes **60.3 µs** from the earlier harness.

## 1. The corrected ladder, on driver master

`bench/db/bench_entity_driver_ladder.py`, eight rungs, each removing one named layer, on this
operation's own shape. 14 blocks × 500, arms rotated, outputs compared every block, all arms over
one pooled connection. Artefact: `runs/entity_driver_20260809/ladder_patched.json`.

**The first finding is that the plan's own numbers no longer hold.** C1 and C2 were sized against
PyMongo 4.12, where `find_one` → `Database.command` → held-connection was 79.1 → 54.8 → 27.4 µs.
Driver master has already absorbed much of that: `_checkout` and `Pool.checkout` are now
hand-written context-manager classes rather than `@contextlib.contextmanager` generators, and both
the CMAP telemetry and the command telemetry are behind enablement fast paths. Re-measured on
master, C1 and C2 together are worth about a quarter of the driver term, not the two thirds the
4.12 ladder projected.

Quiet-box figures, patched driver (client CPU, µs):

| rung | CPU | step | what the step removes |
|---|---|---|---|
| `find_one` | 66.8 | — | |
| `run_op` | 62.4 | 4.4 | implicit session, fast-path body |
| `retry_read` | 54.4 | 8.0 | cursor command assembly, monitoring, unpack, `Response` |
| `sel_checkout` | 45.4 | 9.0 | the retryable-read wrapper |
| `checkout` | 43.3 | 2.1 | server selection (already cached in this build) |
| `conn_sess` | 29.8 | **13.5** | **pool checkout, checkin, `_ClientCheckout`** |
| `conn_cmd` | 27.0 | 2.8 | session application, cluster-time gossip |
| `raw` | 17.6 | 9.4 | `conn.command` machinery |

## 2. Leads

### C2 — serve simple `find_one` without a cursor · **kept**

`find_one` sends `limit -1`, which sets `singleBatch`, so the reply cannot carry a live cursor.
`Collection._find_one_single_batch` builds the `_Query` and reads `firstBatch` directly; anything
outside `{filter, projection, session}` still goes through `Cursor`, as do load-balanced clients.

Paired, 14 blocks × 500, `runs/entity_driver_20260809/fastpath_ab_v2.json`:

| instrument | before | after | saved | blocks |
|---|---|---|---|---|
| retired instructions | 349,919 | 277,211 | 72,696 (20.8%) | 14/14 |
| client CPU | 77.5 µs | 66.4 µs | 12.1 µs (15.6%) | 14/14 |
| wall | 167.7 µs | 157.8 µs | 10.4 µs (6.2%) | 14/14 |

Wire-identical including key order, and the CommandStarted/CommandSucceeded events match field for
field. 1004 driver tests pass, including the CMAP connection-monitoring spec suite.

Branch `find-one-fast-path`, draft PR `carsontung666/mongo-python-driver#1`.

**A blank-context review to driver-team standards found ten defects, all fixed.** Worth recording
because most were invisible from the benchmark:

1. *Blocker.* `_run_operation` is annotated as returning `Response`, so unpacking a tuple from it
   failed `mypy --strict` with ten errors. Fixed by calling `_retryable_read` directly, which also
   removed a wrapper — the reason the numbers above are better than the first measurement
   (10.9 → 12.1 µs).
2. The legacy `$explain` modifier makes the server reply with an explain document rather than a
   cursor, so `find_one({"$query": …, "$explain": True})` raised `KeyError('cursor')` where it used
   to return the plan.
3. A `Collection` subclass overriding `find()` was silently bypassed.
4. A falsy non-`None` session raised `AttributeError`: `Cursor` tests the session for truth, the
   fast path tested it for `None`.
5. Both projection forms at once raised a different `TypeError` message.
6. An implicit session passed explicitly kept `_attached_to_cursor` set.
7. An empty non-`dict` filter (`SON()`, `RawBSONDocument`) was published verbatim in
   `CommandStartedEvent` instead of `{}` — contradicting the claim that the events are unchanged.
8. The gate comment gave the wrong reason for excluding `skip`, `collation`, `batch_size` and
   `allow_disk_use`.
9. An unreachable branch used `cursor["ns"]` where the cursor path uses `.get("ns")`.
10. The tests asserted that options reached the server but never that the fallback *happened*, never
    compared key order, and never exercised a session's own fields.

Each of 2–7 was re-verified against the inlined upstream body afterwards and now matches exactly.
The tests now assert the fast path is taken and not taken, compare key order, and compare `lsid` and
`afterClusterTime` under a causally consistent session.

**Against the bar this is still a partial result.** 15.6% of client CPU clears it; 6.2% of the
operation's wall does not. C2 alone is not enough, which is why the leads below were opened.

### C1a — reuse the last server selection · **kept, but below the bar**

Selection runs per operation and, against an unchanged topology, re-derives the answer it reached
last time. Remembering `(description, selector, server)` for single-candidate selections is sound:
`TopologyDescription` is replaced rather than mutated on any change, and a `Server` is created once
per address for as long as that address is in the description.

Paired in-process, cache invalidated before each uncached call, 12 blocks × 400:
**14,313 instructions, 4.0%, 12/12 blocks.** Roughly 3.2 µs of client CPU.

It also **broke an observable, which was caught and fixed**: skipping the description scan skips the
selection STARTED log message, so after the first operation every subsequent one lost it. Measured
directly — 0 STARTED against 5 SUCCEEDED over five cached operations. The shortcut is now off
whenever server-selection logging is enabled. The driver's own
`test_server_selection_logging` could not have caught this here: those tests fail identically on
stock master in this environment, for reasons unrelated to the change.

Branch `cache-server-selection`. **No PR opened**: 4.0% of the driver's client work is about 1.9% of
the operation, below the bar, and it touches a spec-governed area. Recorded, not pushed for review.

### C1b — pool checkout and checkin · **not attempted, and it is the largest remaining item**

**13.5 µs, 27% of the remaining 49.2 µs driver term, 8.3% of the operation.** Bigger than anything
else left, and the only single change that would take C2 from single-digit to double-digit against
wall.

Decomposed (instructions, at roughly 3,600 per µs on this box):

| | instr |
|---|---|
| `_ClientCheckout` wrapper | 9,232 |
| `_PoolCheckout` wrapper | 4,775 |
| `Pool._get_conn` + `Pool.checkin` | 34,295 |
| — of which lock and condition acquisitions | 10,642 |
| — of which `_perished(conn)` | 3,443 |
| — of which two `os.getpid()` fork checks | 646 + 2 syscalls |

**The structural finding that makes this tractable**: `Pool.lock`, `Pool.size_cond` and
`Pool._max_connecting_cond` are three `Condition` views over *one* mutex —
`_create_condition(self.lock)` at `pool.py:654,674,682`. The eight acquire/release pairs per
operation are eight fragments of the same critical section, and the ones separated only by
non-blocking Python (L1–L4 in `_get_conn`, M1–M3 in `checkin`) can be merged without changing what
is mutually excluded. `_perished` and `connect` must stay outside.

Not attempted here because it is a rewrite of the CMAP checkout path and the accounting
(`requests`, `active_sockets`, `operation_count`) deadlocks if it is wrong. The safety net exists:
`test/test_connection_monitoring.py` runs the CMAP spec tests, which are built to catch exactly
that. This is the next thing to do.

### Not worth doing, measured

- **Retryable-read wrapper**, 9.0 µs. `_ClientConnectionRetryable` is constructed with 16 attributes
  per operation and, on the success path, does nothing. An optimistic first attempt with fallback
  into the retry machinery would recover most of it, but the exception handling
  (`ServerSelectionTimeoutError`, error labels, deprioritised servers, CSOT) is where retryable
  reads are actually specified, and getting it wrong is silent.
- **Implicit `ClientSession` per operation**, 22,338 instructions. `SessionOptions(causal_consistency=False)`
  is rebuilt every time and is immutable — a shared instance saves 2,534 instructions (0.9%).
  `_Transaction(None, client)` is built for every implicit session at 2,417 instructions and an
  implicit session can never be in a transaction, but `_select_server` touches `_transaction` on
  every operation, so making it lazy does not help.
- **`Topology.receive_cluster_time`** takes the topology lock on every reply even when the reply
  carries no `$clusterTime`, 1,826 instructions. An early return is free and correct; it is 0.6%.
- **`os.getpid()` twice per operation** (confirmed by `strace -c`: 2 getpid, 2 poll, 1 sendto,
  2 recvfrom per operation). About 646 user instructions plus two syscalls. Real but tiny.

### Fallback lead 1 — the exclusive phase partition of the server's 45.4 µs · **it already existed**

The hand-off and `get_entity.md` both said this pass had never been run and was the first thing to
do. **It had been run**: `runs/bottleneck_20260806/decomp_get_node/get_entity_phases.txt`, written by
the same `phases.py` classifier on the same day as `get_node_phases.txt`, over the same 83,543
samples, summing to 100.000% / 45.354 µs. Nobody had looked in the `decomp_get_node` directory for a
`get_entity` file.

Checked rather than assumed: its rows agree with the independent inclusive pass to three decimals
where the two overlap (projection `parseAndAnalyze` 1.998% vs 2.00%, collection acquisition 8.027%
vs 7.99%, release 2.044% vs 2.04%).

What it says: **transport 39.6% and command dispatch 20.6% are 60.2% of the operation; all query
work together is 22%.** The two specific targets it exposes are now `get_entity.md` §4 M4 (plan-cache
key build and lookup, 1.185 µs, on a path that skips planning) and M5 (projection dependency
analysis, 1.412 µs, under a `PROJECTION_SIMPLE` plan that cannot need it). Both are real and both
are small — together 5.7% of server CPU, about 1.8% of the operation.

### Confirmed dead, not re-litigated

Syscall fusing (−0.50%, inside noise) and hot-spot hunting (240 functions, largest non-socket frame
0.026 s of 1.066 s) were settled before this session and were not revisited.

## 3. What the driver lane can and cannot deliver

The operation is 173.8 µs of wall in this harness, of which 79.0 µs is client CPU and 17.6 µs is the
irreducible hand-written OP_MSG exchange. **The whole driver term is 61.4 µs, so removing all of it
would move the operation by about 35%.** C2 has taken 10.9 µs of that. C1b is worth another 13.5.
Everything else identified is 9 µs or less apiece and sits in spec-governed machinery.

So the honest ceiling for this lane is around 24 µs — C2 plus C1b — or roughly 14% of the operation,
and reaching it requires the pool-checkout rewrite. That is a smaller prize than the plan's 62.2 µs,
for the documented reason: **the plan's figure was measured against PyMongo 4.12, and driver master
has already taken much of it.**

## 4. Artefacts

| file | what |
|---|---|
| `bench/db/perfcount.py` | per-thread retired-instruction counter |
| `bench/db/bench_entity_driver_ladder.py` | the eight-rung ladder |
| `bench/db/bench_find_one_fastpath.py` | paired A/B for C2 |
| `runs/entity_driver_20260809/ladder_patched.json` | ladder, 14 × 500, quiet box |
| `runs/entity_driver_20260809/fastpath_ab.json` | C2 A/B, 14 × 500, quiet box |

The driver work is in `/home/junyao/code/mongo-python-driver`, branches `find-one-fast-path` and
`cache-server-selection`, pushed to `carsontung666/mongo-python-driver`. Neither is upstream-ready
and no PYTHON- ticket exists for either.
