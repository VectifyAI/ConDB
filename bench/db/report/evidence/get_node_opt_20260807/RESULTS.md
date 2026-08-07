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

r1 and r2 are **withdrawn**; see [Withdrawn runs](#withdrawn-runs). Everything
below is r3–r6.

| run | artifact | inputs | control arm | harness |
|---|---|---|---|---|
| r3 | `arms_r3_control.json` | 64, first in sort order | yes | pre-fix |
| r4 | `arms_r4_sample.json` | 512, sampled across all 10M | yes | pre-fix |
| r5 | `arms_r5_fixed.json` | 64, first in sort order | yes | fixed |
| r6 | `arms_r6_fixed_sample.json` | 512, sampled across all 10M | yes | fixed |

20 blocks × 500 iterations per arm. The four runs are **consecutive on one box,
not independent** — same host, same state, minutes apart. They are replicates
against harness and cohort variation, not against anything systemic.

Instruments:

* `wall_us` — `time.perf_counter`.
* `ccpu_us` — `time.process_time`. **Whole-process**, not per arm: the process
  holds one `MongoClient` per arm plus a probe and their monitor threads are in
  the total. Valid as a paired delta, since the background is identical for
  every arm within a block; not valid as one arm's absolute.
* `scpu_us` — mongod CPU on **this arm's own connection thread**, the first
  field of `/proc/<pid>/task/<tid>/schedstat` (`sum_exec_runtime`), the thread
  identified by the `connectionId` from `hello`. Each arm has its own
  `MongoClient` with `maxPoolSize=minPoolSize=1`. `other_conn_us` — CPU on
  every *other* mongod connection thread — is 0.000 for every arm of every run.

## Result

Absolutes, µs per operation, r3 / r4 / r5 / r6:

| arm | wall | client CPU | server CPU |
|---|---|---|---|
| baseline | 168.5 / 166.9 / 168.7 / 177.0 | 76.5 / 74.5 / 76.0 / 78.0 | 74.8 / 73.6 / 74.6 / 78.5 |
| `+ command` | 147.2 / 146.5 / 151.8 / 156.0 | 54.8 / 54.5 / 56.3 / 56.4 | 73.5 / 72.3 / 75.6 / 79.6 |
| `+ pinned` | 122.7 / 121.2 / 123.7 / 128.2 | 29.5 / 28.8 / 29.0 / 30.3 | 73.5 / 71.9 / 73.5 / 75.4 |
| `+ idhack` | **99.8 / 98.7 / 100.0 / 107.6** | 29.6 / 28.9 / 29.6 / 29.9 | **48.3 / 48.6 / 48.2 / 51.2** |

Paired per-block medians, blocks favouring the arm out of 20:

| comparison | wall | server CPU | client CPU |
|---|---|---|---|
| full stack vs baseline | **−40.9 / −40.5 / −41.4 / −39.4%**, 20·20·20·20 | −34.5 / −34.2 / −36.7 / −33.2%, 20·20·20·20 | −46.6 / −45.4 / −46.4 / −46.4 µs, 20·20·20·20 |
| full stack vs **control** | −39.7 / −40.9 / −40.9 / −39.1%, 20·20·20·20 | **−32.3 / −33.7 / −34.2 / −32.2%**, 20·20·20·18 | −45.7 / −46.7 / −47.0 / −47.4 µs, 20·20·20·20 |
| `command` vs baseline | −13.9 / −12.7 / −10.6 / −12.1%, 20·19·20·18 | −2.6 / −2.6 / +1.5 / +0.7%, 12·11·9·10 | **−23.2 / −21.0 / −19.5 / −21.1 µs**, 20·20·20·20 |
| `pinned` vs `command` | −16.8 / −17.9 / −18.8 / −17.5%, 20·20·20·20 | −0.3 / −2.9 / −3.0 / −3.0%, 11·13·13·12 | **−25.6 / −25.3 / −27.1 / −25.5 µs**, 20·20·20·20 |

**Stacked: 39–41% of wall time, 60–62% of client CPU, and 32–34% of server CPU
against the strictest control.**

### Intervals

Percentile 95% paired-bootstrap intervals on the median per-block delta,
100,000 resamples of whole blocks with replacement, arm and baseline kept
together within a block, seed 20260807 (`analyze_get_node_opt.py`,
`intervals.json`). Post-processing of the retained per-block data; no database
was touched.

| comparison | metric | r3 | r4 | r5 | r6 |
|---|---|---|---|---|---|
| full stack vs baseline | wall | −40.9 [−42.8, −40.1] | −40.6 [−41.9, −39.7] | −41.4 [−42.5, −39.4] | −39.5 [−40.3, −37.0] |
| full stack vs **control** | server CPU | **−32.3 [−35.5, −31.3]** | **−33.7 [−35.8, −32.4]** | **−34.2 [−35.3, −32.6]** | **−32.2 [−35.0, −28.5]** |
| `command` vs baseline | client CPU µs | −23.2 [−24.8, −19.1] | −21.0 [−22.9, −18.4] | −19.5 [−21.0, −18.4] | −21.1 [−22.1, −20.0] |
| `pinned` increment | client CPU µs | −25.6 [−26.5, −24.6] | −25.3 [−26.2, −24.4] | −27.1 [−27.9, −26.8] | −25.5 [−28.0, −24.1] |
| control vs baseline | server CPU | −4.8 [−7.7, **+0.2**] | −0.9 [−1.9, **+1.4**] | −2.7 [−5.0, **+0.8**] | +0.4 [−6.5, **+3.6**] |
| `command` vs baseline | server CPU | −2.6 [−5.9, **+1.8**] | −2.6 [−4.1, **+2.8**] | +1.5 [−1.4, **+4.0**] | +0.7 [−3.0, **+12.7**] |
| `pinned` increment | server CPU | −0.3 [−2.4, **+2.8**] | −2.9 [−4.5, **+0.1**] | −3.0 [−4.5, **+1.6**] | −3.0 [−11.7, **+2.4**] |

**Every effect claimed above has an interval excluding zero in all four runs,
and every quantity called a null has an interval containing zero in all four.**
The three server-CPU nulls are the load-bearing ones: they are what says the
freshness control is inert and that the two client-side changes do not reach the
server.

## Each change against its prediction

| change | predicted | measured | verdict |
|---|---|---|---|
| `Database.command` for `find_one` | 24.4 µs client CPU (`get_node.md`:108) | **19.5–23.2 µs** | undershoots by ~10% |
| pool-checkout fast path | 27.3 µs client CPU | **25.3–27.1 µs** | lands |
| string `_id` → `IDHACK` | 30–37% server CPU | **32–34%** vs control | lands, low-middle |

**The `IDHACK` range does not close upward.** `get_node.md` §5D carries the
claim as 30–37% rather than a point because the retained measurement
(`report/evidence/review_20260807/v6_idhack.json`, −36.7%) queried the
collection's existing `ObjectId` `_id`, which is not the proposed schema. On
the schema actually built here it is 32–34% against a same-age control — inside
the predicted range, at its lower middle, not above it.

Nor was there a string-key penalty to overcome. The `+4.6 µs` figure that
motivated the range is `v6_idhack.json` `probe_str_vs_probe_oid.scpu_us.med_us`,
which carries `n_neg` 5 of 14 and `min_pct/max_pct` −11.46 / +22.67 — straddling
zero. It was never an established penalty.

## Verification

### The client-side arms do not move server CPU

`command` and `pinned` change nothing the server sees, so they must not move
server CPU. They do not: `command` gives −2.6 / −2.6 / +1.5 / +0.7% on 12, 11,
9 and 10 of 20 blocks; the `pinned` increment gives −0.3 / −2.9 / −3.0 / −3.0%
on 11, 13, 13 and 12 of 20. Coin-flip block counts and signs that change across
runs.

`idhack`'s server-CPU effect is the opposite shape — 18–20 of 20 blocks in every
run, in one consistent direction — which is what a real server-side change looks
like on this instrument.

### Freshness is not the mechanism

`layout2_view_idhack` is written minutes before it is measured while
`layout2_view` has been resident for weeks, so a cheaper read could be
WiredTiger cache state rather than the `IDHACK` plan.
`prepare_get_node_control.py` builds `layout2_view_control`: the same `$out`
copy at the same moment, `_id` left as an `ObjectId`, `allops_tree_node`
rebuilt, so **the baseline query runs against it unchanged**. It is a fifth arm
inside the same rotation, not a cross-run comparison.

| control vs baseline | wall | server CPU | client CPU |
|---|---|---|---|
| r3 (64 inputs) | −3.0%, 12/20 | −4.8%, 13/20 | −2.2 µs, 13/20 |
| r4 (512 inputs) | −0.0%, 10/20 | −0.8%, 11/20 | +0.2 µs, 8/20 |
| r5 (64 inputs) | −1.1%, 12/20 | −2.7%, 12/20 | −0.1 µs, 10/20 |
| r6 (512 inputs) | −0.3%, 10/20 | +0.4%, 10/20 | +1.3 µs, 8/20 |

Coin-flip in all four, and the sign turns over. A same-age copy is not
measurably cheaper to read.

The control conflates two things — it is both same-age *and* carries 2 indexes
where the baseline carries 5 — but since it is indistinguishable from the
baseline, both are excluded at once. **All headline `IDHACK` figures above are
quoted against this control, not against the shipped collection**, which costs
1–2 points and is the conservative choice.

### The effect is not an artifact of the input cohort

r3 and r5 use the 64 lowest `node_id`s — what the earlier campaigns used, a
narrow adjacent working set. r4 and r6 draw 512 inputs sampled across all 10M
documents. Full stack against control: −32.3 / −33.7 / −34.2 / −32.2% server
CPU. The cohort does not move it.

### Correctness

Verified before timing **and again after**, with the harness exiting non-zero on
any mismatch: every arm returns the identical seven-field tuple for every input.
The baseline is the reference and is excluded from the comparison count — an
earlier version compared it against itself and inflated the count by one arm's
worth.

Winning plans recorded live: baseline / `command` / `pinned`
`PROJECTION_SIMPLE → FETCH → IXSCAN`; `idhack` `PROJECTION_SIMPLE → IDHACK`.

The fast path engaged rather than silently falling back: 10,463–10,526
fast-path uses against 1–2 full checkouts in every run.

Collection builds: `idhack_prepare.json` — 10,000,000 documents, count matched,
`IDHACK` asserted, seven-field projection compared field-by-field against the
baseline on 500 sampled nodes, 46.5 s. `control_prepare.json` — 10,000,000
documents, 5,927.1 MB, exactly the baseline's size as expected when `_id` is
untouched.

## Fairness

* **The re-keyed collection is larger**: 5,976.1 MB against 5,927.1 MB,
  `avgObjSize` 597 against 592, i.e. **+5 bytes per document** — an 11-character
  string encodes to 16 BSON bytes against an `ObjectId`'s 12. The `FETCH` side
  is not flattered. (An earlier draft said "+1 byte"; that was wrong.)
* **Its `_id` index is also larger** than the index it replaces: 136.6 MB
  against `allops_tree_node`'s 128.1 MB. The index-traversal side is not
  flattered either.
* All three plans do identical data work: `nReturned` 1, `totalKeysExamined` 1,
  `totalDocsExamined` 1 on all three collections.
* PostgreSQL's arm already reaches this row through a unique btree on exactly
  this key, so the change removes an asymmetry rather than creating one.

### Where the arms are *not* like-for-like, stated

**The `idhack` arm sends a different command, and part of its effect is not the
plan.** Captured with a `CommandListener`:

```
baseline  303 B  find filter hint projection limit singleBatch lsid $db
command   303 B  find filter projection hint limit singleBatch lsid $db
idhack    266 B  find filter     projection limit singleBatch lsid $db
```

`idhack` drops `hint` and carries a one-field filter instead of two — 37 bytes,
12% shorter. **The removal is required, not chosen**: any hint disqualifies
`IDHACK` (`query_utils.cpp:52-59`). But `get_node.md` §6 prices hint removal at
−1.29% of server CPU on its own, so the arm is a bundle — `IDHACK` plus no hint
plus a shorter command — and the label "string `_id` → `IDHACK`" claims slightly
more than the arm isolates.

**`command` is not byte-identical to `find_one`.** Same field set, same 303-byte
encoded length, but `hint` and `projection` are in the opposite order. The claim
"byte-identical", which appears in `report.tex` and in earlier drafts of this
work, is wrong. Immaterial to the result; wrong in a document going to driver
engineers.

**The `pinned` increment contains more than pool checkout.** `PinnedReader`
calls `Connection.command` directly, bypassing `Database._command`
(`database.py:748-769`), so the 25.3–27.1 µs also includes that wrapper and the
per-operation implicit-session acquire and release. The matching prior in
`get_node.md`:109 describes exactly this arm, so the number is consistent — but
a pool-side fast path alone would deliver less.

**`maxPoolSize=1` cuts both ways.** It is the cheapest pool the baseline can
check out of — one connection, never contended, never grown — which makes the
`pinned` win a lower bound in that respect. But it also means the pinned arm
never revalidated: 1–2 full checkouts over ~10,500 operations means no heartbeat
changed the topology during any run, so the arm paid no re-selection cost
either. It is a scope limit, not a clean bound.

**The index saving is 102 MB, not 5.3 GB.** An earlier draft compared
`totalIndexSize` between the two collections and reported "5,448.4 MB to
111.3 MB". That is wrong: 4,662.2 MB of the baseline's index bytes is
`layout2_rootcause_exact_cover`, an unrelated experiment's index that this
proposal neither removes nor touches. The real ledger:

| index | MB | fate under this proposal |
|---|---|---|
| `layout2_rootcause_exact_cover` | 4,662.2 | unaffected |
| `path_1_node_id_1` | 280.3 | unaffected |
| `allops_tree_parent_path` | 267.3 | unaffected |
| `allops_tree_node` | 128.1 | **removed** |
| `_id_` | 110.5 | **replaced by a 136.6 MB string `_id_`** |

Net: 238.6 MB → 136.6 MB, a **102 MB** saving.

## Limitations

**`pinned` is a prototype and must not ship in application code.** It reaches
into PyMongo private API (`client._topology`, `Server.checkout`,
`_MongoClientErrorHandler`) and would break on a driver upgrade. Its purpose is
to show that the 27 µs is real and safely reachable, so that the change can be
proposed *inside* PyMongo. Two gaps found while building it argue the same
point — that this work belongs to the driver's authors:

* **SDAM error handling was missing in the first version.** `Pool.checkout`
  takes a `_MongoClientErrorHandler` and runs it from its own
  `except BaseException` branch (`pool.py:1117-1127`); exiting the checkout
  context with `(None, None, None)` skips that branch, so `Topology.handle_error`
  never ran. After a network error or a NotPrimary reply the server would not be
  marked Unknown, the pool not cleared, its generation not bumped, and the
  server session not marked dirty — which the sessions spec forbids. The failure
  is self-concealing: the two checks the fast path relies on to notice trouble,
  topology identity and pool generation, are exactly the state `handle_error`
  would have changed. **Now fixed**, with the handler built lazily in the except
  branch so the happy path pays nothing. The measurements above are unaffected —
  no arm raised.
* **CMAP events are still dropped.** `Pool.checkout` publishes
  `ConnectionCheckOutStarted`, `ConnectionCheckedOut` and `ConnectionCheckedIn`
  (`pool.py:1082-1112`); the fast path emits none, so pool metrics and
  connection logging go dark for every operation it serves. Irrelevant to the
  benchmark, a spec violation for anything shipped.

**A pinned connection starves the pool.** Found by deadlocking on it: three
readers sharing one `maxPoolSize=1` client hang forever. Against the default
`maxPoolSize=100` it is a 1% reduction, but the failure mode is a hang rather
than an error. A pool-side version has to bound the hold — the cheap form is to
check the pool's waiter count and check the connection back in — and that is not
implemented here.

**Single-threaded only.** One pinned connection cannot serve concurrent
operations.

**No PostgreSQL arm.** `psycopg` is not installed in this environment and
installing it on a shared box was out of scope, so the gap arithmetic below uses
the report's existing PostgreSQL figures rather than a fresh paired measurement.

**Projection, not a measurement.** The report's headline table is MongoDB
0.196 ms against PostgreSQL 0.092 ms, 2.13× (`report.tex:356`). This campaign's
baseline is 167–177 µs, a different cohort and harness, so the 39–41% reduction
cannot be subtracted from 196 µs directly. *If* the ratio carried, it would put
MongoDB near 117 µs and the ratio near 1.27×. That is an extrapolation and is
not evidence.

## Withdrawn runs

`arms.json` (r1) and `arms_r2.json` (r2) are retained for the record and **must
not be quoted**.

r1 started 10 seconds after a 46.5 s `$out` wrote a 6 GB collection. Its first
eight blocks have an inflated baseline — wall 261 / 333 / 317 / 328 / 305 / 318 /
225 / 197 µs against 173–187 for blocks 8–19 — while the other three arms are
flat throughout. It is not a rotation artifact: the baseline ran in first, last,
third and second position in blocks 0–3 and is inflated in all four. Eight of
twenty blocks is enough to move a median, and the original summary medianed over
all twenty with no screen. Recomputed on blocks 8–19, r1's `command` client-CPU
delta falls from 25.5 to 21.7 µs and its `IDHACK` server-CPU effect from 38.1%
to 35.1%.

r2 carries the same defect in a different place: spikes at blocks 7, 8 and 12
across `baseline`, `command` and `pinned` (baseline wall 330 / 313 / 245).

Both were caught by the blind review, not by the harness. Two guards were added
in response and both are in r5 and r6:

* a `--settle` delay, 30 s by default, before the first block;
* an outlier screen — drop a block if any arm's wall exceeds 1.5× that arm's own
  median — with the dropped block indices recorded in the output, and the
  unscreened figures kept alongside under `paired_vs_baseline_all_blocks`. The
  screen dropped nothing in either r5 or r6.

Two further harness defects were fixed at the same time: the block order was a
cyclic shift, which fixes arm *adjacency* and therefore lets one arm's deferred
garbage be collected inside a systematically determined neighbour's block, and
is now a seeded permutation; and the per-block liveness check read the loop
variable left over from the last iteration, so it checked one input rather than
the block — the run-level guarantee is now the post-run equality pass.

## Reproducing

```
python3 prepare_get_node_idhack.py --out runs/get_node_opt/idhack_prepare.json
python3 prepare_get_node_control.py

python3 bench_get_node_opt.py --with-control \
        --out runs/get_node_opt/arms_r5_fixed.json
python3 bench_get_node_opt.py --with-control --input-mode sample --inputs 512 \
        --out runs/get_node_opt/arms_r6_fixed_sample.json

python3 analyze_get_node_opt.py --out runs/get_node_opt/intervals.json
(cd runs/get_node_opt && sha256sum *.json RESULTS.md > SHA256SUMS)
```

Both prepare scripts verify an existing collection instead of rebuilding unless
`--force` is given. `$sample` is not seedable server-side, so the sampled runs'
exact cohort is recorded under `run.node_ids` rather than being reproducible
from `--seed`; `--seed` fixes the block order and the subset selection only.

## Files

| file | what |
|---|---|
| `opt_get_node.py` | the three optimizations, as readers with a common interface |
| `prepare_get_node_idhack.py` | builds and verifies `layout2_view_idhack` |
| `prepare_get_node_control.py` | builds `layout2_view_control`, the freshness control |
| `bench_get_node_opt.py` | the paired harness |
| `analyze_get_node_opt.py` | paired-bootstrap intervals over the retained blocks; post-processing only |
| `runs/get_node_opt/idhack_prepare.json`, `control_prepare.json` | build and verification records |
| `runs/get_node_opt/arms_r{3,4,5,6}*.json` | per-block timings, block orders, summaries, paired deltas, input cohorts |
| `runs/get_node_opt/intervals.json` | bootstrap intervals, with each run's own SHA-256 |
| `runs/get_node_opt/SHA256SUMS` | digests of every artifact in the directory |
| `runs/get_node_opt/arms.json`, `arms_r2.json` | withdrawn, retained for the record |

Evidence cited from elsewhere in the tree lives at
`bench/db/report/evidence/review_20260807/` (`v2_driver_paired.json`,
`v6_idhack.json`) and `bench/db/report/ops/get_node.md`.

## State left behind

| | |
|---|---|
| `bench.layout2_view_idhack` | created, 10M docs, 5,976 MB data + 137 MB index. Additive; `layout2_view` untouched |
| `bench.layout2_view_control` | created, 10M docs, 5,927 MB data + 238 MB index. **Control only — drop once this result is accepted** |
| profiler `slowms` | 0 → 100 for each run window, restored to 0 and read back every time |
| MongoDB server | unmodified, stock 7.0.34 |
| PyMongo | unmodified, 140 files verified against installed `RECORD` hashes |
| existing repo scripts | none modified |
