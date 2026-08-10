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

- The stock `find_one` measures **79.0 µs of client CPU** on a quiet box. The 4.12-era ablation, a
  different harness on a different shape, put `find_one` at **79.1 µs**.

  (The stock arm's absolute reading moves a few per cent between processes — 334,152 instructions in
  one run, 349,919 in another — which is why nothing here claims an absolute before-and-after across
  runs. Every figure below is a paired delta taken inside one process.)
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

### C1a — reuse the last server selection · **withdrawn**

Remembering `(description, selector, server)` for single-candidate selections looked sound:
`TopologyDescription` is replaced rather than mutated on any change, and a `Server` is created once
per address. Measured 14,313 instructions, 4.0%, 12/12 blocks.

**A blank-context review killed it, and was right to.** Three findings, of which the first is
enough:

- **It silently bypasses `MongoClient(server_selector=...)`**, a public option since 3.4, which is
  passed as `custom_selector` into `apply_selector`. It is not in the cache key and is not invoked
  on a hit, so "called on every selection" becomes "called once per `TopologyDescription`".
  Measured: 50 `find_one` calls invoked the selector **once** against 50 on the parent commit; a
  round-robin selector over three mongoses sent all 60 selections to one server instead of 20 each.
- A hit can hand out a `Server` whose pool `close()` has already closed, because the unlocked read
  skips the lock that used to serialise selection against `close()` — turning a documented
  `InvalidOperation` into an internal `_PoolClosedError`.
- **The benefit does not generalise.** Reads use the client's `Primary()` instance and writes the
  module-level `writable_server_selector`; a one-entry cache keyed by identity evicts on every
  alternation. Measured: 100 pure `find_one` calls hit 99 times, 50 interleaved read/write calls hit
  **once in 100**. And the `len(servers) == 1` guard means it never populates for a multi-node
  replica set read or a multi-mongos cluster — the deployments where selection cost matters.

So the 4.0% was measured on the one workload shape that hits, and the change breaks a public API on
the others. PR closed with these reasons. **Every combined figure below that included C1a has been
withdrawn with it.**

### C1b — pool checkout and checkin · **attempted; 3.0%, and the layer turns out not to be removable**

The layer costs **13.5 µs, 27% of the remaining 49.2 µs driver term**, which made it the only
candidate that could have taken C2 from single-digit to double-digit against wall. It does not.

**The structural finding**: `Pool.lock`, `Pool.size_cond` and `Pool._max_connecting_cond` are three
`Condition` views over *one* mutex (`_create_condition(self.lock)`, `pool.py:654,674,682`). The
eight acquire/release pairs per operation — five in `_get_conn`, three in `checkin` — are eight
fragments of a single critical section. Taking it once is the same mutual exclusion.

Built on branch `pool-checkout-fast-path`: `Pool._checkout_idle` takes the mutex once and re-checks
everything the long path checks except `conn_closed()`, which is a syscall and does not belong under
the pool mutex; anything that does not hold returns `None` and the long path runs in full. `checkin`
merges its three the same way.

Paired in-process, fast path disabled for the control arm, 12 blocks × 400:

| | control | fast | saved |
|---|---|---|---|
| checkout + checkin | 60,042 | 48,950 | 11,116 instr (**18.5%**, 12/12) |
| whole operation | 368,159 | 357,105 | 11,116 instr (**3.0%**, 12/12) |

**So the acquisitions are only about a fifth of the layer.** The other four fifths are the
`_ClientCheckout` (9,232 instr) and `_PoolCheckout` (4,775) objects and accounting the pool
genuinely has to do — `requests`, `active_sockets`, `operation_count`, `active_contexts`,
generation and idle checks. Merging the two wrapper classes might recover another ~1 µs. Nothing
here reaches the bar.

**The driver's own tests caught a defect in the first version**: `test_pooling.py::
test_get_conn_reused_connection_rolls_back_on_cancel` failed because the fast path incremented the
pool counters before `active_contexts.add`, the one statement that can raise, leaving them
half-updated. Reordered so nothing is touched until after it. 132 pool and CMAP spec tests pass, and 1129 pass
across the broader suite — the three that fail (`test_discovery_and_monitoring`) fail identically on
stock master in this environment, and a fourth, `test_collection.py::test_create`, was leftover
state from a run killed earlier and passes once the stale collection is dropped.

Branch `pool-checkout-fast-path`, draft PR `#3`, same reasoning as C1a: below the bar alone,
proposed as one of three.

**A second blank-context review, of the pool change specifically, verified the premise and found
eight more defects.** The premise held — the three `Condition`s are views over one mutex in both the
sync and async builds, and cross-view `notify()` is legal in both, with the `_waiters` lists staying
distinct so notify targeting survives. The defects were about what merging changed *around* the
mutex, and the first was real: **the merged `checkin` released the maxPoolSize slot before closing a
stale connection**, so a waiting thread could open a replacement while the old socket was still
open and a pool could briefly hold twice `maxPoolSize` sockets — worse in the async build, where
closing awaits. Only the return-a-healthy-connection case is merged now; everything else falls
through to the untouched long path. Also fixed: a `checkOutStarted` event that could be left
unpaired, the `_perished` predicate duplicated with nothing pointing back, a third copy of the
events gate where a `_should_log` property already existed, and a comment the unasync tool mangled
from "waiter" to "witer" via its `aiter -> iter` mapping. Six tests added, none of which existed.

**An upstream bug found in passing, not introduced here:** `operation_count` leaks on every *failed*
checkout — `_get_conn` increments it but neither its `except BaseException` clause nor the
`_raise_*` paths decrement it, and only a pid change resets it. It feeds least-operations server
selection. Measured on both the branch and the base commit (597 vs 635 leaked in the same
wait-queue scenario), so it is pre-existing. Recorded in PR `#3`'s limits as deserving its own
ticket.

Its decomposition, retained because it is the evidence for the conclusion above (instructions, at
roughly 3,600 per µs on this box):

| | instr |
|---|---|
| `_ClientCheckout` wrapper | 9,232 |
| `_PoolCheckout` wrapper | 4,775 |
| `Pool._get_conn` + `Pool.checkin` | 34,295 |
| — of which lock and condition acquisitions | 10,642 |
| — of which `_perished(conn)` | 3,443 |
| — of which two `os.getpid()` fork checks | 646 + 2 syscalls |

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

## 2b. The three together — **withdrawn**

This section reported that the three changes measured together removed 21.1 µs of client CPU
(24.1%) and 28.4 µs of wall (14.3%), and that the three measured separately summed to within 0.2%
of the combined figure. **Both claims are withdrawn.** An audit found three things wrong with them
and each on its own is disqualifying.

**The 0.2% agreement was run selection.** Two combined runs exist,
`all_three_ab.json` and `all_three_ab_v2.json` — same harness, same driver build, same 14 × 500,
same entity id, one minute apart:

| | instructions saved | client CPU | wall | wall blocks |
|---|---|---|---|---|
| run 1 | 104,433 (28.0%) | 25.6 µs | 29.4 µs (15.0%) | 13/14 |
| run 2 | 98,372 (26.8%) | 21.1 µs | 28.4 µs (14.3%) | 14/14 |

Only run 2 was reported. Against the same parts-sum, run 1 disagrees by **6.0%**, thirty times the
claimed agreement. The parts-sum itself used **11,184** instructions for C1b where §2 of this same
document reports **11,116**; 11,184 appears nowhere else. And the check was close to tautological
anyway: the three changes touch disjoint code paths, so additivity was expected before measuring.

**The control arm is not the stock driver.** `bench_entity_driver_all.py` writes a class attribute
on `Pool` on every iteration in both arms, which bumps the type's version tag and defeats CPython's
attribute specialisation for every later `Pool` lookup; the control additionally pays an extra
Python frame and a declining `_checkout_idle` call that a stock driver would not have. The
absolutes the percentages are ratios against are therefore not the driver's cost.

**And one of the three is now withdrawn** — C1a bypasses a public option, above. A combined figure
that includes it does not describe anything that could be proposed.

**What is left standing is the one measurement that was built as a paired A/B against a faithful
control**: C2 alone, 12.1 µs of client CPU, 15.6%, 14/14 blocks
(`fastpath_ab_v2.json`), whose control arm is `find_one`'s own body inlined and checked against the
driver's source at startup. C1b's 3.0% stands as an instruction-count measurement of that change in
isolation. **The two have never been measured together against a faithful control, and no combined
figure is claimed.**

## 3. What the driver lane can and cannot deliver

The operation is about 167 µs of wall in this harness, of which 77.5 µs is client CPU and 17.6 µs is
the irreducible hand-written OP_MSG exchange, so the whole driver term is about 60 µs.

**What survives review:**

| lead | measured | instrument | status |
|---|---|---|---|
| C2 `find_one` without a cursor | **12.1 µs client CPU, 15.6%**, 14/14 blocks | paired A/B, faithful control | PR open |
| C1b pool mutex taken once | 3.0% of instructions, 12/12 blocks | in-process toggle, no retained harness | PR open |
| C1a server-selection reuse | 4.0% of instructions | in-process toggle, no retained harness | **withdrawn** |

**Three honest limits on that table.**

*The units are not addable and are not added here.* C2's figure is client CPU; C1b's is retired
instructions. Converting one into the other assumes the removed code has the same instructions per
cycle as the operation as a whole, which is exactly the conversion §0 says is never performed. An
earlier version of this file did it anyway to produce a "5.6 µs combined" for M1; that figure is
withdrawn.

*C1b's numbers are not reproducible from anything retained.* They were taken with ad-hoc in-process
scripts that were never saved, so nothing in `bench/db/` re-derives 11,116 or 3.0%. The C2 figure
does not have this problem — `bench_find_one_fastpath.py` and its JSON are both committed. This is a
process failure, and the C1b figure should be read as unverified until a harness exists.

*`exclude_kernel` inflates every instruction-derived percentage.* The counter omits the operation's
syscall work from both numerator and denominator, which is why the same blocks read 26.8% in
instructions and 24.1% in CPU. An instruction percentage is therefore an upper bound on the CPU
percentage, not an estimate of it.

**The shape of the conclusion is unchanged.** After C2, client CPU is about 66 µs against a 17.6 µs
floor, and the ~49 µs between them is spread across the retryable-read wrapper, the connection
pool's accounting, implicit session creation and `conn.command` — four spec-governed subsystems,
none worth more than 9 µs. `get_entity.md` §2b says the same thing from the server side.

**Corrected by the same-transport grid (`get_entity.md` §2d): on wall, at equal transport, the
driver is the biggest lever on this operation.** Same box, same rows, same TCP loopback: pymongo
4.13 139.2 µs wall against psycopg unprepared 61.2, with 76.8 of the 78.0 µs difference being
client CPU. Driver master plus the `find_one` fast path measures 98.9 µs on that rig — about 40 µs
of the driver gap already removed — and the hand-written OP_MSG floor puts most of the remaining
~27 µs within mechanical reach, at the cost of the driver's feature set. The server upgrade
(6.74 µs server CPU, §2c) is real but second to this on wall.

## 3b. The pre-merge reviews, and what they cost the claims

Two further blank-context reviews ran at maintainer strictness against the two open PRs, each
instructed to treat the newest commit — written under review pressure — as unreviewed code.

**PR #1 (`find_one`): request-changes, now addressed.** The retry test added by the previous round
was demonstrated to flake (`$clusterTime` is re-gossiped by the reconnect its own failpoint forces)
and to pass with the fast path deleted — it now drops the volatile field and asserts the path was
taken. The unreachable non-zero-cursor-id branch is now pinned by forging a reply and asserting the
killCursors. The PR body carried three stale sections from before the fixes; rewritten. The review
also verified, claim by claim, the equivalence table — including a zero-drift regeneration of the
sync files and a clean `mypy --strict` — and found no behavioural divergence on any gate-accepted
input.

**PR #3 (pool): request-changes, with a real blocker in my own fix.** The rollback added after the
previous review covered only the last two statements: the `popleft` and the three counter
increments sat before the `try`, so an interrupt between them — the exact class the commit named —
still leaked a maxPoolSize permit forever. Demonstrated by injection. Every mutation now sits
inside the `try` with a progress marker; verified by injection at two points with pool state
byte-identical before and after. Also added, from the same review: the `requests`-at-max decline
test (that branch is load-bearing, not defensive — the long checkin queues the connection and
releases the slot in separate critical sections), a CMAP-pairing test for the raise path, the
missing pool-untouched assertions, and a changelog entry. One gap is disclosed in the PR rather
than papered over: nothing asserts the merged checkin branch is taken, because there is no clean
seam to assert it without instrumenting production code.

The scoreboard for the lane after five review rounds: **28 defects found by reviewers, 27 fixed,
one disclosed as an open question to maintainers; two of the 28 were blockers in code I wrote to
fix earlier findings.** That last clause is the argument for the process.

## 4. Artefacts

| file | what |
|---|---|
| `bench/db/perfcount.py` | per-thread retired-instruction counter |
| `bench/db/bench_entity_driver_ladder.py` | the eight-rung ladder |
| `bench/db/bench_find_one_fastpath.py` | paired A/B for C2 |
| `runs/entity_driver_20260809/ladder_patched.json` | ladder, 14 × 500, quiet box |
| `runs/entity_driver_20260809/fastpath_ab_v2.json` | C2 A/B after review fixes, 14 × 500, quiet box |
| `report/evidence/entity_driver_20260809/get_entity_phases.txt` | the exclusive server-side partition |

The driver work is in `/home/junyao/code/mongo-python-driver`, branches `find-one-fast-path`,
`cache-server-selection` and `pool-checkout-fast-path`, pushed to
`carsontung666/mongo-python-driver`. None is upstream-ready and no PYTHON- ticket exists for any of
them. Only `find-one-fast-path` has a PR.
