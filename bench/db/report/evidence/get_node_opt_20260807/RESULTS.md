# `get_node` optimization — three stacking changes, measured

Three changes to a hinted `find_one` on `(tree_id, node_id)`, each one layer on
top of the last:

| arm | change | side |
|---|---|---|
| `command` | `Database.command` in place of `find_one` | client, public API |
| `pinned` | server selection and pool checkout amortized across operations | client, prototype only |
| `idhack` | the natural key promoted to a string `_id` | schema |

**Neither MongoDB nor PyMongo is modified.** The server is the stock `mongo:7`
image; the only server state touched is the profiler `slowms`, set to 100 for
each run window and restored to 0 with the value read back. PyMongo is the
installed 4.12.0 with no patch and no monkey-patching — 140 of its `.py` files
verified against the installed `RECORD` hashes, 0 modified.

## Identity

| | |
|---|---|
| MongoDB | 7.0.34, container `condb_mongo`, image `mongo:7` |
| image digest | `mongo@sha256:4b5bf3c2bb7516164f6dcb44acce4fdcb428abfe5771a1128304a0f34ab9ff7c` |
| image ID | `sha256:dce1a146801e912688bd7e60e67a8fda266cd4afb530e9f30c5ed4f11d4ba0c5` |
| argv | `[mongod]`, no auth, reached over the published port 57017 |
| PyMongo | 4.12.0, unmodified (140 `.py` files vs installed `RECORD`, 0 differ) |
| Python | 3.14.5 |
| host | dual-socket Intel Xeon Gold 6418H, 48 physical cores, 96 hardware threads, 1,007 GB RAM |
| kernel | Linux 6.8.0-84-generic |
| pinning | none; processes were not CPU- or NUMA-pinned |
| dataset | `bench.layout2_view`, 10,000,000 documents, 5,927.1 MB, `avgObjSize` 592, one `tree_id` (`base`) |

Artifact digests are in `SHA256SUMS`; each run's own digest is also recorded
inside `intervals.json`.

**Where these files live.** The scripts write into `bench/db/runs/get_node_opt/`,
which `.gitignore:78` excludes. The committed copy of every artifact, including
this document, is `bench/db/report/evidence/get_node_opt_20260807/`, byte-equal
and carrying its own `SHA256SUMS`. Paths written `runs/get_node_opt/...` below
are what the scripts produce; read them from the evidence directory.

**Cross-references to work not on this branch.** This branch is based on
`b29e097`, the last commit pushed for the report campaign. Three things this
document cites are in commits that are still local and are not here:
`report/ops/get_node.md` (the per-operation analysis these three optimizations
were derived from, and whose section 5D prediction they test),
`report/evidence/review_20260807/v6_idhack.json` (the earlier ObjectId `IDHACK`
probe) and `.../v2_driver_paired.json` (the earlier `Database.command`
measurement). `report/report.tex` is present. Nothing in the result depends on
those three: every figure quoted here is computed from the artifacts in this
directory, and `intervals.json` and `SHA256SUMS` pin them. The citations are
there so the priors can be checked once the report branch lands.


## Runs

**r1-r6 are superseded and must not be quoted.** r1 and r2 were withdrawn for
post-build contamination; r3-r6 were produced by a harness version that is not
in this commit and therefore cannot be reproduced from it. All are retained
under `superseded/`. See [Superseded runs](#superseded-runs).

Everything below is r7-r12, all produced by the code committed here.

| run | artifact | inputs | arms |
|---|---|---|---|
| r7 | `arms_r7_head.json` | 64, first in sort order | 6 |
| r8 | `arms_r8_sample.json` | 512, sampled | 6 |
| r9 | `arms_r9_head.json` | 64, first in sort order | 6 |
| r10 | `arms_r10_sample.json` | 512, sampled | 6 |
| r11 | `arms_r11_nohint.json` | 64, first in sort order | 7, adds `nohint` |
| r12 | `arms_r12_nohint_sample.json` | 512, sampled | 7, adds `nohint` |

20 blocks x 500 iterations per arm, arm order a seeded permutation per block.
The runs are **consecutive on one box, not independent** -- same host, minutes
apart. They are replicates against harness and cohort variation, not against
anything systemic.

Arms, each the previous one plus one change:

| arm | what it is |
|---|---|
| `baseline` | `bench_all_ops_layouts.py:146-168` verbatim: hinted `find_one` |
| `command` | `Database.command`, no `Cursor` |
| `unpinned` | `Connection.command` + explicit session, full checkout every operation |
| `pinned` | the same, on a connection held across operations |
| `idhack` | the same, against the string-`_id` collection, no hint |
| `control` | the **baseline query**, on a same-age copy of the collection |
| `nohint` | the **baseline query** with the hint dropped and nothing else (r11-r12) |

`unpinned` exists to split what used to be a single `command` -> `pinned` step.
That step bundles three things -- `Database._command`'s wrapper, the
per-operation implicit session, and server selection plus pool checkout -- and
an earlier version of this document credited all three to "pool checkout".

Instruments:

* `wall_us` -- `time.perf_counter`.
* `ccpu_us` -- `time.process_time`. **Whole-process**, not per arm: the process
  holds one `MongoClient` per arm plus a probe, and their monitor threads are in
  the total. Valid as a paired delta; **not** valid as one arm's absolute, and
  for that reason no percentage of client CPU is quoted anywhere below -- only
  microsecond deltas.
* `scpu_us` -- mongod CPU on **this arm's own connection thread**, the first
  field of `/proc/<pid>/task/<tid>/schedstat` (`sum_exec_runtime`), the thread
  identified by the `connectionId` from `hello`. Each arm has its own
  `MongoClient` with `maxPoolSize=minPoolSize=1`. `other_conn_us` -- CPU on
  every *other* mongod connection thread -- is 0.000 for every arm of every run.

## Result

Absolutes, microseconds per operation, r7 / r8 / r9 / r10:

| arm | wall | client CPU | server CPU |
|---|---|---|---|
| `baseline` | 173.4 / 175.8 / 169.9 / 171.6 | 78.7 / 79.5 / 76.7 / 77.4 | 76.5 / 77.9 / 75.2 / 76.5 |
| `+ command` | 152.3 / 152.7 / 148.6 / 149.9 | 57.3 / 57.2 / 55.1 / 55.2 | 76.2 / 76.5 / 74.6 / 76.2 |
| `+ unpinned` | 143.2 / 141.4 / 136.3 / 139.7 | 45.2 / 44.4 / 42.8 / 43.7 | 77.9 / 77.4 / 73.8 / 76.7 |
| `+ pinned` | 132.2 / 127.8 / 130.6 / 127.0 | 31.3 / 30.8 / 31.4 / 30.6 | 83.0 / 77.1 / 79.8 / 76.5 |
| `+ idhack` | **103.7 / 106.1 / 101.2 / 105.1** | 31.8 / 31.3 / 29.9 / 30.5 | **50.5 / 53.5 / 49.6 / 52.6** |
| `control` | 175.8 / 174.7 / 168.2 / 172.4 | 80.8 / 78.3 / 75.8 / 76.5 | 76.3 / 78.6 / 73.4 / 77.5 |

### Headline

Percentile 95% paired-bootstrap intervals on the median per-block delta,
100,000 resamples of whole blocks with replacement, arm and baseline kept
together within a block, seed 20260807 (`analyze_get_node_opt.py`,
`intervals.json`).

| full stack vs `control` | r7 | r8 | r9 | r10 |
|---|---|---|---|---|
| wall | −39.7 [−42.1, −37.9] | −39.1 [−40.6, −38.5] | −39.7 [−40.8, −39.3] | −40.1 [−40.8, −38.7] |
| server CPU | −32.8 [−36.5, −29.5] | −32.2 [−33.8, −30.4] | −33.4 [−35.2, −32.2] | −33.4 [−34.0, −32.0] |
| client CPU µs | −47.6 [−49.5, −46.4] | −47.6 [−48.1, −45.2] | −45.8 [−47.9, −45.3] | −46.1 [−47.2, −45.1] |

All 20/20 blocks, all intervals excluding zero.

**Stacked: 39–40% of wall time, 32–33% of server CPU, and 46–48 microseconds of
client CPU, against the strictest control.** 172 microseconds down to 104.

### Where the client-side saving actually comes from

The three-way split, client CPU, paired medians and 95% intervals:

| step | r7 | r8 | r9 | r10 |
|---|---|---|---|---|
| `baseline` → `command` | −21.2 [−23.9, −18.8] | −22.3 [−23.6, −20.6] | −22.1 [−24.0, −20.8] | −20.6 [−22.1, −20.0] |
| `command` → `unpinned` (wrapper + implicit session + retryable-read machinery) | −11.6 [−13.4, −10.5] | −13.0 [−13.5, −12.0] | −12.6 [−13.3, −11.5] | −12.6 [−13.9, −11.0] |
| **`unpinned` → `pinned` (pool checkout + server selection)** | **−13.9 [−15.3, −12.7]** | **−13.5 [−14.8, −12.5]** | −11.4 [−12.6, **+4.8**] | **−13.1 [−13.9, −12.2]** |

**The pool-side change is worth about 13 microseconds, not the 26–27 an earlier
version of this document credited to it.** That 26 was the whole
`command` → `pinned` step, which bundles three things; only the last row is the
pool. Against the 27.3 microsecond prior for a "pool-checkout fast path", the
isolated pool-side change **undershoots by roughly half**.

r9's interval straddles zero: its outlier screen dropped blocks 0, 3 and 8, and
the 17 that remain are noisier. The other three runs are tight and consistent.

**This arm probably still flatters the pool side slightly.** `unpinned` calls
`Topology.select_server` and `Server.checkout` directly with `handler=None`,
while the driver's own per-operation path also constructs a
`_MongoClientErrorHandler` and carries operation ids and CSOT bookkeeping. A
faithful per-operation path would cost a little more than `unpinned` does, so
the true pool-side saving is **at least** 13 microseconds. An independent
decomposition by a different method put it at 20.7; that figure could not be
reproduced with this A/B, and the discrepancy is unexplained. **13 is the number
this commit's artifacts support.** What is not method-dependent is the total:
`command` → `pinned` is 25.1–26.6 microseconds in all four runs.

## Verification

### `IDHACK` is worth less than the arm's label suggests

The `idhack` arm cannot carry a hint -- any hint disqualifies `IDHACK`
(`query_utils.cpp:52-59`) -- so part of what it gains is hint removal, not the
plan. r11 and r12 price that with a `nohint` arm: the baseline query, hint
dropped, nothing else changed.

| | r11 (64 inputs) | r12 (512 inputs) |
|---|---|---|
| `nohint` vs `baseline`, server CPU | −0.14%, 10/20 blocks | −4.50%, 16/20 blocks |
| `idhack` vs `baseline`, server CPU | −34.3%, 20/20 | −29.7%, 20/20 |
| **`idhack` vs `nohint`, server CPU** | **−33.6%, 20/20** | **−28.0%, 20/20** |

Hint removal is itself unstable — a coin flip on the narrow cohort and −4.5% on
the wide one — so it cannot be subtracted as a constant. Net of it, the
`IDHACK` plan alone is worth **28–34% of server CPU**, against a 30–37% prior.
It straddles the bottom of the predicted range rather than sitting in its
middle, which is what an earlier version of this document claimed.

### The client-side arms mostly do not move server CPU — but `pinned` sometimes does

`command`, `unpinned` and `pinned` change nothing the server sees, so none of
them should move server CPU. Server-CPU deltas, paired medians with 95%
intervals:

| step | r7 | r8 | r9 | r10 |
|---|---|---|---|---|
| `baseline` → `command` | −0.4 [−2.1, +2.3] | −1.2 [−2.5, +1.4] | −1.7 [−2.5, +1.5] | +2.2 [−0.2, +4.4] |
| `command` → `unpinned` | +3.4 [−0.4, +6.0] | +0.8 [−0.6, +4.0] | −2.7 [−5.3, −0.4] | −3.0 [−6.1, +1.2] |
| `unpinned` → `pinned` | **+8.5 [+2.8, +12.9]** | +0.1 [−2.2, +2.3] | **+7.5 [+2.6, +39.8]** | +0.1 [−1.2, +1.6] |

`command` behaves: four intervals, all containing zero. `unpinned` is one
interval out of four just excluding zero, in the direction of *more* server CPU,
which the other three do not support.

**`pinned` is not a null.** In r7 and r9 — both the 64-input cohort — pinning the
connection costs **7–9% more server CPU**, with intervals excluding zero and only
5 and 2 of 20 blocks favouring the arm. In r8 and r10 — both the 512-input
cohort — it is flat. This is reported because it is what the data says; the
mechanism is not established. The plausible one is arrival rate: the pinned arm
removes ~14 microseconds of client work between requests, so the same connection
thread is driven harder, and per-operation CPU on a thread that never idles need
not equal that of one that does. **It does not cancel the win** — wall time still
falls 39–40% — but the claim that a client-side change leaves the server
untouched is false for this arm, and a pool-side fast path should be measured for
it rather than assumed clean.

### Freshness is not the mechanism

`layout2_view_idhack` is written minutes before it is read while `layout2_view`
has been resident for weeks, so a cheaper read could be WiredTiger cache state
rather than the `IDHACK` plan. `prepare_get_node_control.py` builds
`layout2_view_control`: the same `$out` copy at the same moment, `_id` left as an
`ObjectId`, `allops_tree_node` rebuilt, so **the baseline query runs against it
unchanged**. It is a fifth arm inside the same rotation, not a cross-run
comparison.

| `control` vs `baseline` | wall | server CPU | client CPU µs |
|---|---|---|---|
| r7 | +0.9%, 8/20 | −0.4 [−1.8, +2.6], 10/20 | +1.8 [−0.1, +3.1], 6/20 |
| r8 | −0.5%, 12/20 | +1.7 [−0.7, +4.0], 7/20 | −1.4 [−2.9, +0.2], 13/20 |
| r9 | −0.9%, 12/20 | −1.8 [−5.1, −0.3], 13/20 | −1.1 [−2.2, +1.1], 12/20 |
| r10 | +0.4%, 9/20 | +3.1 [−0.1, +5.8], 7/20 | +0.7 [−1.8, +2.2], 10/20 |

Coin-flip block counts, signs that turn over between runs, and one interval in
twelve just excluding zero — about what a genuine null produces at this interval
width. A same-age copy is not measurably cheaper to read.

The control conflates two things — it is both same-age *and* carries 2 indexes
where the baseline carries 5 — but since it is indistinguishable from the
baseline, both are excluded at once. **All headline figures above are quoted
against this control, not against the shipped collection**, which is the
conservative choice.

### The effect is not an artifact of the input cohort

r7, r9 and r11 use the 64 lowest `node_id`s, a narrow adjacent working set. r8,
r10 and r12 draw 512 inputs sampled across the collection. Full stack against
control, server CPU: −32.8 / −32.2 / −33.4 / −33.4%. The cohort does not move the
headline.

It does move two things, and both are recorded above: hint removal (a null on the
narrow cohort, −4.5% on the wide one) and the `pinned` server-CPU anomaly
(present on the narrow cohort, absent on the wide one).

### Correctness

Verified before timing **and again after**, with the harness exiting non-zero on
any mismatch: every arm returns the identical seven-field tuple for every input.
The baseline is the reference and is excluded from the comparison count. Elements
compared: 2,240 (r7, r9), 17,920 (r8, r10), 2,688 (r11), 21,504 (r12); `all_equal`
true in both the before and after pass of every run.

The comparison is `tuple(document.get(field) for field in FIELDS)`, so a missing
field and a null field are indistinguishable. All three collections hold
whole-document copies, so there is no live risk, but the gate is weaker than
"identical seven-field tuple" implies.

Winning plans recorded live: `baseline` / `command` / `unpinned` / `pinned`
`PROJECTION_SIMPLE → FETCH → IXSCAN`; `idhack` `PROJECTION_SIMPLE → IDHACK`.

The fast path engaged rather than silently falling back: 10,525–11,421 fast-path
uses against 2–3 full checkouts per run. The 2–3 are heartbeats, not faults; see
the topology note under Limitations.

Collection builds: `idhack_prepare.json` — 10,000,000 documents, count matched,
`IDHACK` asserted, seven-field projection compared field-by-field against the
baseline on 500 sampled nodes, 46.5 s. `control_prepare.json` — 10,000,000
documents, 5,927.1 MB, exactly the baseline's size as expected when `_id` is
untouched.

## Fairness

* **The re-keyed collection is larger**: 5,976.1 MB against 5,927.1 MB,
  `avgObjSize` 597 against 592, i.e. **+5 bytes per document** — an 11-character
  string encodes to 16 BSON bytes against an `ObjectId`'s 12, and the copy keeps
  `tree_id` and `node_id` as fields as well. The `FETCH` side is not flattered.
  (An earlier draft said "+1 byte"; that was wrong.)
* **Its `_id` index is also larger** than the index it displaces: 136.6 MB
  against `allops_tree_node`'s 128.1 MB.
* All arms do identical data work: `nReturned` 1, `totalKeysExamined` 1,
  `totalDocsExamined` 1 on all three collections; identical WiredTiger options
  (`snappy`, `internal_page_max` 4 KB, `leaf_page_max` 32 KB).
* PostgreSQL's arm already reaches this row through a unique btree on exactly
  this key, so the change removes an asymmetry rather than creating one.

### Where the arms are *not* like-for-like, stated

**The `idhack` arm sends a different command.** Captured with a
`CommandListener`:

```
baseline  303 B  find filter hint projection limit singleBatch lsid $db
command   303 B  find filter projection hint limit singleBatch lsid $db
idhack    266 B  find filter     projection limit singleBatch lsid $db
control   311 B  find filter hint projection limit singleBatch lsid $db
```

`idhack` drops `hint` and carries a one-field filter instead of two — 37 bytes,
12% shorter. The removal is required, not chosen, and the `nohint` arm above
prices what it is worth. The `control` arm's command is 8 bytes **longer** than
the baseline's, because `layout2_view_control` is a longer collection name; that
is against the control's own favour, so it does not weaken the freshness null.

**`command` is not byte-identical to `find_one`.** Same field set, same 303-byte
encoded length, but `hint` and `projection` are in the opposite order. The claim
"byte-identical", which appears in `report.tex` and in earlier drafts of this
work, is wrong.

**`command` also drops retryable reads, and that is priced into its win.**
`find_one` routes through `Cursor._send_message` → `MongoClient._run_operation` →
`_retryable_read` (`cursor.py:1171+`, `mongo_client.py:1896`); `Database.command`
does not. Independently measured at **2.65 microseconds of the ~22 microsecond
`command` win — about 12% of the effect is a failure-semantics change, not the
removal of a `Cursor`.** The baseline's default is `retryReads=True`, so calling
this "the defaults on both sides" is not accurate. Anyone adopting
`Database.command` gives up automatic retry on a transient network error.

**`maxPoolSize=1` cuts both ways.** It is the cheapest pool the baseline can
check out of, which makes the pool-side win a lower bound in that respect. But
the pinned arm also revalidated only 2–3 times per run, so it paid almost no
re-selection cost.

**The index saving is 102 MB, not 5.3 GB.** An earlier draft compared
`totalIndexSize` between the two collections and reported "5,448.4 MB to
111.3 MB". That is wrong: 4,662.2 MB of the baseline's index bytes is
`layout2_rootcause_exact_cover`, an unrelated experiment's index that this
proposal neither removes nor touches. The ledger, read live from `indexSizes`:

| index | MB | fate under this proposal |
|---|---|---|
| `layout2_rootcause_exact_cover` | 4,662.2 | unaffected |
| `path_1_node_id_1` | 280.3 | unaffected |
| `allops_tree_parent_path` | 267.3 | unaffected |
| `allops_tree_node` | 128.1 | **removed** |
| `_id_` | 110.5 | **replaced by a 136.6 MB string `_id_`** |

Net: 238.6 MB → 136.6 MB, a **102 MB** saving. Note that
`idhack_prepare.json` records `index_size_mb: 111.3` for the new `_id_`: that was
its size immediately after the build, before it settled at 136.6. The 136.6,
128.1, 110.5 and `avgObjSize` figures are read live and are **not** in any
retained artifact; neither is the wire capture above. Both are reproducible from
the commands in Reproducing.

## Limitations

**`pinned` is a prototype and must not ship in application code.** It reaches
into PyMongo private API (`client._topology`, `Server.checkout`,
`_MongoClientErrorHandler`, `PoolState`, `ConnectionClosedReason`) and would
break on a driver upgrade. Its purpose is to bound what a pool-side fast path is
worth so the change can be proposed *inside* PyMongo. Three defects found while
building it argue that this work belongs to the driver's authors:

* **It laundered dead connections back into the pool.** `_usable` copied
  `Pool._perished`'s conditions but not its side effects: `_perished`
  (`pool.py:1373-1406`) calls `conn.close_conn(...)` on every failing branch,
  while `_usable` merely returned `False`, so `release()` handed the connection
  to `Pool.checkin`, which saw `closed` false, refreshed its checkin time and
  appended it to `pool.conns`. The refreshed time then put `idle_time_seconds`
  below `_check_interval_seconds`, so the next checkout's own `_perished`
  **skipped its `conn_closed()` probe and re-issued the same dead socket** —
  turning a failure PyMongo would have absorbed by reconnecting into a raised
  `AutoReconnect`. It also defeated `maxIdleTimeMS` outright. **Fixed**, and
  regression-tested: a killed socket is now closed, does not return to the pool,
  and the next command recovers.
* **The check order made that worse, and hid it.** The topology-identity check
  ran first and short-circuited the health checks — and `TopologyDescription`
  identity churns on **every heartbeat**, roughly every 10 s at the default
  `heartbeatFrequencyMS`, so "dead *and* superseded" was the common case rather
  than a corner. Health checks now run unconditionally first. This also corrects
  a claim in an earlier draft that no heartbeat changed the topology during any
  run: the 2–3 full checkouts per run **are** heartbeats.
* **SDAM error handling was missing, and the first fix only half-closed it.**
  `Pool.checkout` runs a `_MongoClientErrorHandler` from its own
  `except BaseException` branch (`pool.py:1117-1127`); exiting the checkout
  context with `(None, None, None)` skips it. The first fix wrapped only the
  command, leaving the acquisition path — where `Pool.connect` uses the handler
  for `contribute_socket(..., completed_handshake=False)` and handshake
  cluster-time gossip (`pool.py:1048-1049, 1059-1060`) — still passing
  `handler=None`, so a handshake or auth failure still ran no
  `Topology.handle_error`. **Now fixed on both paths**, with the handler built in
  `_acquire` and reused by the command path so `handled` is tracked across both,
  exactly as `MongoClient`'s single `with` block tracks it.

Still open in the prototype, and disclosed rather than fixed:

* **CMAP events are dropped.** `Pool.checkout` publishes
  `ConnectionCheckOutStarted`, `ConnectionCheckedOut` and `ConnectionCheckedIn`
  (`pool.py:1082-1112`); the fast path emits none, so pool metrics and connection
  logging go dark for every operation it serves.
* **A pinned connection starves the pool.** Found by deadlocking on it: three
  readers sharing one `maxPoolSize=1` client hang forever. A pool-side version
  has to release when the pool has waiters.
* **`stale_generation` is read without the pool lock**, which `Pool.checkin`
  takes explicitly to avoid racing `Pool.reset()`.
* **Single-threaded only.** One pinned connection cannot serve concurrent
  operations.
* **It costs server CPU on one cohort**, 7–9%, unexplained; see Verification.

**No PostgreSQL arm.** `psycopg` is not installed in this environment and
installing it on a shared box was out of scope, so the arithmetic below uses the
report's existing PostgreSQL figures rather than a fresh paired measurement.

**Projection, not a measurement.** The report's headline table is MongoDB
0.196 ms against PostgreSQL 0.092 ms, 2.13× (`report.tex:356`). This campaign's
baseline is 170–176 microseconds, a different cohort and harness, so the 39–40%
reduction cannot be subtracted from 196 microseconds directly. *If* the ratio
carried, it would put MongoDB near 118 microseconds and the ratio near 1.28×.
That is an extrapolation and is not evidence.

**Priors are off-branch.** The 24.4 and 27.3 microsecond client-CPU priors, the
−1.29% hint figure and the −36.7% `ObjectId` `IDHACK` probe live in
`report/ops/get_node.md` and `report/evidence/review_20260807/`, which are not on
this branch. They are cited for context; no figure here depends on them.

## Superseded runs

Retained under `superseded/`, **none of them quotable**.

**r1, `arms.json` — contaminated.** It began 10 seconds after a 46.5 s `$out`
wrote six gigabytes. Its first eight blocks carry an inflated baseline — wall
261 / 333 / 317 / 328 / 305 / 318 / 225 / 197 microseconds against 173–187 for
blocks 8–19 — while the other three arms are flat throughout. Not a rotation
artifact: the baseline ran in four different positions across those blocks and is
inflated in all four. Recomputed on blocks 8–19, its `command` client-CPU delta
falls from 25.5 to 21.7 microseconds and its `IDHACK` effect from 38.1% to 35.1%.

**r2, `arms_r2.json` — the same defect** at blocks 7, 8 and 12 (baseline wall
330 / 313 / 245), across three arms.

**r3–r6 — not reproducible from this commit.** They were produced by harness
versions that no longer exist: they lack `equality_after`, `block_orders`,
`outlier_screen` and `run.settle_seconds`, were never outlier-screened, and r4
and r6 used a cohort selector that has since been shown to be wrong (below).
Their numbers were consistent with r7–r12, but a figure that cannot be
reproduced from the committed code is not evidence, so every headline in this
document was re-measured on r7–r12.

Guards added in response, all active in r7–r12:

* a `--settle` delay, 30 s by default, before the first block;
* an outlier screen — drop a block if any arm's wall exceeds 1.5× that arm's own
  median — with the dropped indices recorded, the unscreened figures kept
  alongside under `paired_vs_baseline_all_blocks`, and the same rule recomputed
  independently in `analyze_get_node_opt.py` so every run is screened
  identically. It fired once, dropping blocks 0, 3 and 8 of r9;
* block order is a seeded permutation, not a cyclic shift. The shift fixed arm
  *adjacency*, which matters because `gc.enable()` runs outside the timed region,
  so one arm's deferred garbage fell in a systematically determined neighbour's
  block;
* the sampled cohort is now `random.sample` over the whole `$sample` draw. It
  used to be `sorted(set(pool))[:n]`, which silently took the lexicographically
  smallest quarter — in r4, nothing above `node_id` 6,846,721 appeared at all,
  so "sampled across all 10M" was false. The preceding `shuffle` was dead code,
  erased by the `sort`, which also meant `--seed` had no effect on the subset;
* the per-block liveness check read the loop variable left over from the last
  iteration, so it checked one input rather than the block; the run-level
  guarantee is now the post-run equality pass;
* `analyze_get_node_opt.py` seeds each cell with `zlib.crc32`, not `hash()`.
  Python's string `hash` is per-process randomized, so an earlier "seed 20260807"
  did not describe a reproducible computation.

## Reproducing

```
python3 prepare_get_node_idhack.py --out runs/get_node_opt/idhack_prepare.json
python3 prepare_get_node_control.py

python3 bench_get_node_opt.py --with-control --out runs/get_node_opt/arms_r7_head.json
python3 bench_get_node_opt.py --with-control --input-mode sample --inputs 512 \
        --out runs/get_node_opt/arms_r8_sample.json
python3 bench_get_node_opt.py --with-control --out runs/get_node_opt/arms_r9_head.json
python3 bench_get_node_opt.py --with-control --input-mode sample --inputs 512 \
        --out runs/get_node_opt/arms_r10_sample.json
python3 bench_get_node_opt.py --with-control --with-nohint \
        --out runs/get_node_opt/arms_r11_nohint.json
python3 bench_get_node_opt.py --with-control --with-nohint --input-mode sample \
        --inputs 512 --out runs/get_node_opt/arms_r12_nohint_sample.json

python3 analyze_get_node_opt.py --out runs/get_node_opt/intervals.json
(cd runs/get_node_opt && sha256sum *.json RESULTS.md > SHA256SUMS)
```

The live figures quoted in Fairness — index sizes, `avgObjSize`, the wire
capture — come from `db.command("collstats", ...)`'s `indexSizes` and a
`pymongo.monitoring.CommandListener` around one call of each reader.

`$sample` is not seedable server-side, so the sampled runs' draw differs run to
run; the exact cohort is recorded under `run.node_ids`, and `--seed` fixes the
block order and the subset taken from a given draw.

## Files

| file | what |
|---|---|
| `opt_get_node.py` | the optimizations and the measurement-only arms |
| `prepare_get_node_idhack.py` | builds and verifies `layout2_view_idhack` |
| `prepare_get_node_control.py` | builds `layout2_view_control`, the freshness control |
| `bench_get_node_opt.py` | the paired harness |
| `analyze_get_node_opt.py` | paired-bootstrap intervals; post-processing only |
| `idhack_prepare.json`, `control_prepare.json` | build and verification records |
| `arms_r7` … `arms_r12` | per-block timings, block orders, screens, cohorts |
| `intervals.json` | bootstrap intervals, with each run's own SHA-256 |
| `SHA256SUMS` | digests of every artifact here |
| `superseded/` | r1–r6, retained and not quotable |

## State left behind

| | |
|---|---|
| `bench.layout2_view_idhack` | created, 10M docs, 5,976 MB data + 137 MB index. Additive; `layout2_view` untouched |
| `bench.layout2_view_control` | created, 10M docs, 5,927 MB data + 238 MB index. **Control only — drop once this result is accepted** |
| profiler `slowms` | 0 → 100 for each run window, restored to 0 and read back every time |
| MongoDB server | unmodified, stock 7.0.34 |
| PyMongo | unmodified, 140 files verified against installed `RECORD` hashes |
| existing repo scripts | none modified |
