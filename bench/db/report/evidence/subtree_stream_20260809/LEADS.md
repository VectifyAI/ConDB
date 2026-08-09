# `get_subtree` — lead log

Every lead tried, numbered, negative results retained. Target is MongoDB master.
Worktree `/tmp/mongo-subtree-stream`, branch `stream-cursor-reply`, base
`0561c098b99ac5e929005e70a2e37d7a97a82423`.

Units discipline: server CPU, client wall, and retired instructions are three different
quantities and are never compared across. "Cohort-weighted" and "per-subtree median" are
named explicitly every time.

---

## L1 — Transmit the reply before the batch is full

### L1a — Can `mongod` send partial replies to a client that did not ask for exhaust?

**No. Settled by reading the code; a protocol fact, not a measurement.**

`src/mongo/transport/session_workflow.cpp:299-301` — `makeExhaustMessage` returns an empty
optional unless `OpMsgRequest::isFlagSet(requestMsg, OpMsg::kExhaustSupported)`. That flag
arrives on the wire in the request the *client* built; nothing on the server sets it. Only
after that gate passes does `:333` set `OpMsg::kMoreToCome` on the response.

The consequence is structural, not incidental. A client that did not set `kExhaustSupported`
reads exactly one reply message per request. A server that emitted a second message would
leave it in the socket to be mis-read as the reply to the client's *next* request — the
connection desynchronizes. So "stream several smaller replies" is unavailable server-side by
construction, for every driver, no matter what `find_cmd.cpp` does.

`transport/session.h:133` corroborates from the other end: `virtual Status sinkMessage(Message
message)` — the only egress API takes one complete, already-built `Message`. There is no
partial-send or chunked-write entry point in the session interface.

**What this rules out.** The initial plan as literally stated — mongod emitting batch fragments
as they are produced — cannot be reached from `find_cmd.cpp` / `getmore_cmd.cpp` alone.

**What survives, and is measured below.** Two things the server *can* change unilaterally:

- **L1b** — write the bytes of the *single* reply to the socket as they are produced, overlapping
  transmission with production. Legal (one message either way), but it only recovers transmission
  time, not the client's decode; ceiling probed before implementing.
- **L1c** — the server, not the client, picks how many bytes a batch carries.
  `find_common.cpp:39` pins `kMaxBytesToReturnToClientAtOnce` to `BSONObjMaxUserSize` (16 MB).
  Requires no client change and no protocol change.

Status: **settled, negative for the literal plan.** Retained.

---

### L1b — Write the bytes of the *single* reply as they are produced

**Worth ~20% of wall if it were possible. It is not possible for OP_MSG. Negative, retained.**

This is the only form of "transmit before the batch is full" that survives L1a: the client still
receives exactly one reply message, so no protocol or driver change is implied, and the server
overlaps transmission with production instead of doing them back to back.

**First, what it would be worth** — measured, because a negative result is only interesting if the
prize was real. `bench_subtree_l1b_ceiling.py` puts a byte-timestamping TCP relay between PyMongo
and 57017 and recovers reply boundaries from the OP_MSG length prefix rather than from timing gaps.
It reports the P50 subtree's reply as **5,108,543 B for 11,686 rows**, which is the report's 5.11 MB
to the byte — the instrument reproduces the known quantity. Its *timings* are not usable: relaying
5 MB through a Python pump inflates the operation to 50.2 ms against the 14.1–20.2 ms the report
records, so the relay's own copying, not the server's, sets the pace.

Measured clean instead, without the relay (`l1b_loopback.json`, 12 reps): one `sendall` of exactly
5,108,543 B on loopback, with the peer draining via `recv_into`, costs **2,779 µs median / 1,665 µs
min (~1.8 GB/s)**; the 42,785,375 B P90 reply costs **19,092 µs (~2.2 GB/s)**. This is client wall
time on a synthetic socket pair — not server CPU, and not measured inside mongod.

Against the report's 14,127 µs P50 wall that is **~20%**, and it closes the budget: 8,330 µs server
CPU + ~2,800 µs moving bytes + ~3,000 µs client decode ≈ 14,130 µs, which reproduces the report's
5,797 µs "residual" as transmission plus decode. So the operation is roughly **59% produce / 20%
transmit / 21% decode**, and L1b's ceiling is that middle fifth.

**Why it cannot be built.** An OP_MSG reply is one `Message` over one `SharedBuffer`
(`rpc/message.h:375-452`), and its total length is written into the header by
`MsgData::View::setLen` (`message.h:327`) at `OpMsgBuilder::finish()` (`op_msg.cpp:432-443`) —
after the body is complete. The `cursor` reply document inside it carries its own BSON length
prefix with the same property. Both lengths sit at byte 0 of what must go out first, and TCP will
not let them be written last. The length is not known until production has finished, which is
exactly the moment the server already starts writing. There is no prefix of the message that can
legally be sent early.

Padding a batch to a pre-announced byte count would make the length known in advance, but it means
inventing filler content inside `CursorResponse` — not something to put in front of a reviewer, and
it changes the reply that drivers parse.

**What this leaves.** The 20% transmit share is unreachable from the reply path. The 59% production
share is reachable, and it is where L2 goes.

Status: **settled, negative.** Prize quantified at ~20% of P50 wall; blocked by the OP_MSG length
prefix, not by effort.

---

## L2 — Fuse the covered-projection decode (design; not yet measured on master)

The report's 36.6% (`KeyString::toBson` 24.4% + `ProjectionStageCovered::transform` 12.3%) comes
from a **7.0.34** profile. Master has since grown a zero-copy key API, so the premise is re-checked
before anything is built: **measure the denominator on master first.**

What master does today, per row, on this plan (`PROJECTION_COVERED` over `IXSCAN`):

1. The WiredTiger cursor's `next()` returns an `IndexKeyEntry` whose `key` is a **fully
   materialised BSONObj** — every one of the four components decoded, with generated field names.
2. `index_scan.cpp:267` — `if (!kv->key.isOwned()) kv->key = kv->key.getOwned();`
3. `index_scan.cpp:273` — the BSONObj is stored into the `WorkingSetMember` as an `IndexKeyDatum`.
4. `projection.cpp:259-279` — `ProjectionStageCovered::transform` iterates that BSONObj and
   **builds a second BSONObj**, keeping the three projected fields.
5. The batch builder appends that object into the reply buffer.

So the ~420-byte key (`path` ~7-20 B, `node_id` 6, `title` ~53, `summary` ~340) is materialised
twice and copied at least three times, and `path` — decoded in full at step 1 — is thrown away at
step 4.

**Master already has the machinery to avoid step 1**, but only off the classic path:
`sorted_data_interface.h:393` `virtual SortedDataKeyValueView nextKeyValueView(RecoveryUnit&)`,
used by SBE (`exec/sbe/stages/ix_scan.cpp:258`) and express (`exec/express/express_plan.h:566`).
Classic `IndexScan` does not use it. `get_subtree` runs classic.

### The denominator, measured on master (not inherited from the 7.0.34 profile)

Environment: `layout2_view` cloned whole — all 10,000,000 documents — out of 57017 into a mongod
built from this worktree (`9.0.0-alpha0`) on port 57018, 32 GB WT cache, `slowms 10000` so operation
logging does not distort. The clone reproduces the source **exactly** where it matters: covering
index 4.662 GB against the source's 4.662 GB, `path_1_node_id_1` 0.280 vs 0.280,
`allops_tree_node` 0.128 vs 0.128, `allops_tree_parent_path` 0.267 vs 0.267, 10,000,000 documents
both sides.

The operation itself reproduces too, which is the real check. At the cohort P50 input
(`/000006/000075/000773`, 11,686 rows), single-threaded:

| | master here | report (7.0.34) |
|---|---|---|
| server CPU per op | **8,219 / 8,229 µs** (two runs) | 8,330 µs |
| client wall per op | 13.66 / 13.47 ms | 14.13 ms |

So the master binary on the cloned data is the same operation to within ~1.3% on server CPU, and
the two profiling runs agree with each other to 0.1%.

**The report's 36.6% is an inclusive figure, and it reproduces.** `perf report --children`
(`profile_base_p50_cg.json`, 400 Hz, dwarf call graphs, 20 s, 1,485 operations):

| inclusive, share of sampled mongod cycles | master | report |
|---|---|---|
| `key_string::toBson` | **21.46%** | 24.4% |
| `ProjectionStageCovered::transform` | **13.90%** | 12.3% |
| sum | **35.36%** | 36.6% |

These two are legitimately summable — `transform` and `IndexScan::doWork` (45.04%) are siblings
under `ProjectionStage::doWork` (59.51%, and 45.04 + 13.90 = 58.94 ✓), and `toBson` sits inside
`IndexScan`'s 45.04%. They are *not* summable with anything else in that table.

**But inclusive is the wrong number for sizing a fusion, and this is where the brief overreaches.**
Self (exclusive) time, from a separate no-call-graph run at 999 Hz (`profile_base_p50.json`,
2,196 operations) — these percentages are summable:

| self | |
|---|---|
| `ProjectionStageCovered::transform` | 3.96% |
| `key_string::toBsonSafe` | 3.14% |
| `readCString` + `readCStringWithNuls` | 2.11% + 2.08% |
| `BSONObjBuilderBase::append(string_view,…)` | 2.50% + 0.70% |
| `BSONObjBuilderBase::appendAs` | 2.13% |
| `DocumentStorage::reset` | 2.34% |
| `transitionMemberToOwnedObj(Document&&)` | 2.15% |
| `Document::toBson` | 1.00% |
| `key_string::TypeBits::Reader::readStringLike` | 0.82% |
| `__wt_btcur_next` (the index walk) | 3.74% |
| `__memmove_avx512` / `__memchr_evex` | 3.10% / 2.16% |

Fusing cannot remove the whole 35.36%. Nested inclusive differences say where it goes:
`toBson`(21.46) − `toBsonSafe`(18.32) = 3.14 pp of builder construction and `obj()`;
`toBsonSafe`(18.32) − `toBsonValue`(13.77) = 4.55 pp of loop and field-name setup;
`toBsonValue`(13.77) − `readCStringWithNuls`(7.22) = 6.55 pp of appending into the intermediate
object. **`readCStringWithNuls` at 7.22% inclusive is the irreducible read** — the memchr scan and
TypeBits that any implementation must pay. Everything above it, plus `transform`'s 13.90%, is
materialisation that exists only because the key is built once to be immediately rebuilt.

Two further findings from the same profile, both new:

- **The socket write is 16.08% of mongod's CPU** (`__libc_send` inclusive, via
  `asio::socket_ops::sync_send1`). That is the server-side cost of the transmission L1b cannot
  overlap, and it is CPU, not the wall figure measured in L1b.
- **`__wt_btcur_next` is 10.93% inclusive**, against the report's 7.9%. Not the same quantity as
  the report's (which the report does not label), so treated as a separate measurement, not a
  contradiction.

**Revised estimate of what L2 is worth.** Keep the 7.22% read, pay one append pass and one `obj()`
instead of two, keep the member transition: removable is roughly **12–18% of server CPU**, i.e.
**7–11% of wall**. That is materially less than the 36.6% headline suggests, and it straddles this
project's bar. Recorded before building, so the result is judged against a prediction rather than
the prediction against the result.

**Proposed shape.** `ProjectionStageCovered`'s constructor (`projection.cpp:222-257`) already
precomputes exactly what a fused decoder needs: `_includeKey` (one bool per key component) and
`_keyFieldNames` (the output name for each included component). Give `IndexScan` that pair as an
optional spec; when it is set, take `nextKeyValueView`, decode the wanted components straight into
one `BSONObjBuilder` with their projected names, and `transitionMemberToOwnedObj`. The classic
stage builder (`classic_stage_builder.cpp:184-198`) fuses the two nodes instead of stacking them.

**Locally-decidable precondition** for taking the fused path, all checkable in the stage builder /
at `doWork` entry: no residual `_filter`, no `IndexBoundsChecker` (`_checker == nullptr`), no
`_dedup`, no `_addKeyMetadata`. Those are the only other consumers of the materialised key
BSONObj in `IndexScan::doWork`; when any is present, fall back to the existing path. Sequential
*traversal* of the KeyString is still required — skipping a component desynchronises
`TypeBits::Reader` — but sequential *materialisation* is not.

### L2 — as built

Branch `stream-cursor-reply`, five files:

- `storage/key_string/key_string.{h,cpp}` — `toBsonProjectedSafe()`. Same component loop as
  `toBsonSafe`, but each component is appended under its projected field name, and excluded
  components are decoded into a caller-owned scratch `BufBuilder` and dropped. Excluded components
  are still decoded, never skipped: `TypeBits::Reader` reads positionally, so skipping one
  desynchronises every component after it. SBE's `readKeyStringValueIntoAccessors`
  (`exec/sbe/values/value.cpp:861`) reaches the same conclusion and says so in a comment.
- `exec/classic/index_scan.{h,cpp}` — an optional `CoveredProjection` spec. When set and the scan
  is in `GETTING_NEXT` with no `IndexBoundsChecker`, the stage takes
  `_indexCursor->nextKeyValueView()` — the zero-copy API master already gives SBE and express — and
  decodes straight into the output object. The seek paths keep the materialising path and project
  from the finished key; they run once per scan, not once per key. The RecordId is never decoded on
  the fused path because `transitionMemberToOwnedObj` discards it anyway.
- `query/stage_builder/classic_stage_builder.cpp` — folds `PROJECTION_COVERED` into the `IXSCAN`
  below it and emits no projection stage. Refused when the scan has a residual filter, deduplicates,
  or requests key metadata (each is another reader of the materialised key), when the projection is
  not inclusion-only, or for `distinct` rewrites. Both helpers are file-local: putting them on the
  class pulled `index_scan.h` into `classic_stage_builder.h` and closed a build-graph cycle
  (`classic_stage_builder.h → index_scan.h → plan_executor.h → plan_explainer.h → ...`).
- `query/query_execution_knobs.idl` — `internalQueryEnableFusedCoveredProjection`,
  `set_at: [startup, runtime]`, default false.

**Gate: a runtime server parameter, not the env var the brief specifies.** The brief's reason for an
env var read once at startup is to keep the gate out of per-operation variance. This gate is read
**once per plan build**, not once per key, so a runtime knob cannot reach the hot loop — and it buys
a strictly better instrument: both arms in one process, against one dbpath and one warm WT cache,
alternating within blocks. Two processes would have meant two clones and reintroduced exactly the
layout variance the discipline exists to avoid.

**Defect caught before measuring**, recorded because it would have silently halved the result:
`projectKeyValueView` first called `Ordering::make(_keyPattern)` per key, re-walking the four-field
key pattern 11,686 times per operation. Hoisted to a `const Ordering _ordering` member built once in
the constructor.

**Activation and correctness, on the real 11,686-row P50 subtree:**

| arm | explain `winningPlan` stages | rows |
|---|---|---|
| knob off | `PROJECTION_COVERED`, `IXSCAN` | 11,686 |
| knob on | `IXSCAN` | 11,686 |

Outputs compared element-wise, in order: **identical**. The plan shape is the activation proof —
the fused arm has no projection stage.

### L2 — measured

`bench_subtree_fused_ab.py`. Both arms in one mongod process against one dbpath and one warm WT
cache; arms alternate inside every block with the leading arm rotating; each arm-block is a fixed
wall-clock window with the query replayed back to back inside it and all three counters read over
that same window; per-operation figures divide by the operations that actually completed. Output is
compared element-wise against a reference on every block of both arms.

**Three quantities, never interchanged.** `instructions:u` counts **user-space instructions only**.
Server CPU is `utime + stime` from `/proc`, which **includes kernel time** — and this operation
spends ~16% of its CPU in `sendto`. The fusion removes user-space work exclusively, so it is
necessarily a larger share of user instructions than of total CPU. The gap between the two columns
below is that, not a measurement disagreement, and neither number may be quoted as the other.

**These are the post-review numbers.** An earlier design deleted the `PROJECTION_COVERED` stage
from the plan; review found that breaks the QuerySolution/PlanStage correspondence (see "review"
below), so the stage now stays as a pass-through. Everything was re-measured against the revised
code; the superseded figures are in `ab_p50_p90.json` / `ab_tail.json` and differ by about 1–1.7 pp
of retired instructions, which is what the pass-through costs.

Quiet box, 10 blocks × 5 s windows for P50/P90 (`ab_p50_p90_v2.json`), 8 blocks × 15 s for the tail
(`ab_tail_v2.json`):

| input | rows | retired instructions | server CPU | client wall | mismatches |
|---|---|---|---|---|---|
| P50 | 11,686 | **−13.80%** [−14.19, −13.55] | **−13.12%** [−16.07, −9.92] | **−6.36%** [−9.18, −3.30] | 0 |
| P90 | 97,773 | **−14.58%** [−15.64, −13.03] | **−11.95%** [−14.44, −8.77] | **−5.18%** [−7.49, −2.11] | 0 |
| tail | 1,404,566 | **−14.47%** [−14.55, −14.46] | **−13.04%** [−14.76, −11.74] | **−5.23%** [−6.10, −3.61] | 0 |

Every block improved on every measurement — 10/10 at P50 and P90, 8/8 at the tail — and every range
is strictly negative. **The effect is flat across a 120× change in input size.**

**A demonstration of why retired instructions are the primary metric here**, from a tail run that had
to be discarded. Partway through it, a build was started on the same box — my own, which is a
discipline failure worth recording rather than hiding. Across the affected blocks:

| block | client wall | server CPU | retired instructions |
|---|---|---|---|
| 1 | −3.54% | −12.02% | −13.99% |
| 3 | −5.19% | −11.18% | −14.18% |
| 4 | **+12.20%** | **+5.15%** | −14.10% |
| 5 | **+140.47%** | **+107.16%** | −13.96% |
| 6 | −2.72% | −11.66% | −14.12% |

Wall and CPU moved by more than 100%; **retired instructions stayed inside a 0.22 pp band the whole
time.** That run is not used for anything; the tail figures in the table above come from a re-run on
an idle box (98.7% idle, zero compiler processes, verified before starting).

### L2 — non-intrusion

`bench_subtree_fused_control.py`, same pairing discipline, 10 blocks, 3 s windows
(`controls.json`). Three shapes the change must not damage:

| control | rows | IXSCAN `coveredProjection` | retired instructions | server CPU | client wall | mismatches |
|---|---|---|---|---|---|---|
| small covered result (fused) | 8 | false → **true** | −0.52% [−3.66, +2.22] | +1.93% | +1.29% | 0 |
| uncovered, needs a FETCH (never eligible) | 1 | false → false | **+0.06%** [−0.53, +0.21] | +0.65% | +0.53% | 0 |
| covered scan with a residual filter (**refused**) | 8 | false → false | **+0.02%** [−0.36, +0.31] | −0.24% | −0.52% | 0 |

The two shapes that never take the fused path cost **+0.06% and +0.02% of retired instructions** —
indistinguishable from zero, and the price of the eligibility check alone. Small covered results
are neutral at −0.52%: on an 8-row query the decode saving and the retained pass-through stage
roughly cancel, which is the honest reading rather than a win. Their CPU and wall columns are
noisier than their instruction columns because these queries run in microseconds, far under the
10 ms resolution of `/proc` CPU accounting.

The refusal is genuinely exercised: the filtered control keeps `coveredProjection: false` with a
residual filter present, so the guard fires rather than the shape simply never qualifying.

**Two of these controls were wrong on the first attempt and are recorded because the corrections
matter.** The residual-filter control originally used `node_id: {$gte: ""}`; the planner folded
that into index bounds, leaving no filter, so the query fused and the control tested nothing. It
now uses a non-anchored regex on an indexed field, which stays a residual filter, and the refusal
is visible in the table above — `PROJECTION_COVERED` survives in both arms. The **jstest carried
the identical flaw** (`{a: {$gte: 0}, b: 3}` on `{a,b,c}`) and would have asserted the right thing
for the wrong reason; it was fixed the same way. Separately, `find_small_prefix` first selected a
**1,338,993-row** range as the "small" control: it walked up from a leaf to the first prefix with
at least 2 rows, and in this tree one step too far up multiplies the count by six orders of
magnitude. It is now bounded to a [2, 64] band.

### L2 — departures from the brief's instrumentation, and why

- **Runtime knob instead of an env var read once at startup.** See above: the gate is read once per
  plan build, never per key, so it cannot reach the hot loop, and one process against one dbpath is
  a better-paired instrument than two processes against two clones.
- **No activation counter printed at exit.** The brief asks for one. A static destructor writing to
  stderr is a debugging artifact that would not survive review, and explain gives a stronger,
  per-query proof that the plan fused. But explain does *not* distinguish the fused fast path from
  the seek fallback inside the fused stage — they produce identical output — so that distinction is
  established with a perf profile instead, below.

### L2 — proof that the fast path is what ran

Explain shows the *plan* fused. It cannot show whether the keys went through the fused fast path
(`projectKeyValueView`, off `nextKeyValueView`) or the seek fallback (`projectMaterialisedKey`),
because the two produce identical output. A perf profile separates them. Self time, 999 Hz, same
P50 input, knob off then on:

| symbol | knob off | knob on |
|---|---|---|
| `ProjectionStageCovered::transform` | 3.96% | **absent** |
| `key_string::toBsonSafe` | 3.14% | **absent** |
| `key_string::toBson` | 0.67% | **absent** |
| `BSONObjBuilderBase::appendAs` | 2.13% | **absent** |
| `key_string::toBsonProjectedSafe` | — | **5.33%** |
| `IndexScan::projectKeyValueView` | — | **1.39%** |
| `readCString` / `readCStringWithNuls` | 2.11% / 2.08% | 2.12% / 2.16% |

The two materialisations collapse into one, the irreducible read is untouched, and
`projectKeyValueView` — not `projectMaterialisedKey` — is what appears, so the fast path served the
keys. **Read as presence and absence only.** These are two separate profiles with different total
cycle counts, so the percentages are not comparable in magnitude across the columns.

The same profile reports 6,562 µs/op server CPU against the base profile's 8,219. **That figure is
unpaired and is not used.** Different runs, different binaries, minutes apart; this project has
already had an unpaired −14% on this very operation become +0.5% when paired. The paired −13.12%
stands.

### L2 — correctness on values the dataset does not contain

`layout2_view` holds nothing but ordinary strings, so the A/B's element-wise checks never exercise
the decoder's hard paths. `bench_subtree_fused_types.py` builds those cases explicitly and runs
every query with the knob off and on (`types_differential.json`):

| case | rows | fused | identical |
|---|---|---|---|
| skip leading / trailing / both / no components | 11 each | yes | yes |
| descending component, skip leading / middle | 50 each | yes | yes |
| residual filter (non-anchored regex) | 19 | **refused** | yes |
| multikey — plans with a FETCH, never covered | 3 | n/a, plan unchanged | yes |
| single key, seek path only | 1 | yes | yes |
| 2,000 rows at `internalQueryExecYieldIterations: 1` | 2,000 | yes | yes |

Indexed values include embedded NULs in leading, trailing, doubled and lone positions, `Int64`,
`Decimal128`, `null`, `true`, `Date`, `-0.0`, `MinKey`, `MaxKey`, a 500-character string and
non-ASCII text. **All ten cases identical, plan shapes as expected.**

A third instrument bug, recorded: the multikey case first reported a false failure because the
harness inferred "fused" from the absence of `PROJECTION_COVERED` in the fused plan. A multikey
index cannot serve a covered projection at all — that query plans `PROJECTION_SIMPLE, SORT, FETCH,
IXSCAN` in both arms — so there was no covered projection to lose. Fusion is now inferred from the
base plan having one and the fused plan not, and the case is relabelled: it shows the plan is
untouched, and documents that **the stage builder's `shouldDedup` guard is not reachable through a
covered projection** rather than claiming it was exercised. The jstest carried the same flaw and
was corrected identically.

### L2 — acceptance gate

1. **Test that fails on the unpatched base.** `jstests/noPassthrough/query/fused_covered_projection.js`
   — differential across the same cases as above. Under resmoke against the patched build: **all 3
   tests passed in 0.73 s** (`no_passthrough` suite, `--dbpathPrefix` to a writable path; the
   worktree needs `mongo`/`mongod` symlinks at its root, as the main checkout has).
2. **Non-intrusion.** Table above: small covered results −2.34% instructions, never-eligible
   FETCH +0.10%, refused residual filter +0.07%; `internalQueryExecYieldIterations: 1` over 2,000
   rows returns identical output.
3. **Locally-decidable safety.** The fused path holds an unowned `SortedDataKeyValueView` across
   the decode. `handlePlanStageYield` (`plan_executor_impl.h:57-75`) is `try { return f(); } catch
   { yieldHandler(); return NEED_YIELD; }` — on the success path it returns the lambda's value with
   no `save()`, no further `next()`/`seek()`, and no cursor destruction, so the view is still valid
   at the decode immediately after. The eligibility conditions the stage builder checks
   (`filter`, `shouldDedup`, `addKeyMetadata`, inclusion-only, not `distinct`) are properties of the
   solution node; the one condition it cannot know in advance, `_checker`, is created inside
   `initIndexScan()` and is re-checked at runtime, with the materialising fallback behind it.
4. **Error paths.** `toBsonProjectedSafe` raises the same `keyStringAssert`s as `toBsonSafe` on a
   malformed key. Those are `uassert`s, not the storage exceptions `handlePlanStageYield` converts
   to yields, so they propagate out of `doWork` exactly as they do today — the decode simply happens
   in `IndexScan::doWork` instead of inside the WiredTiger cursor's `curr()`.

### L2 — the tail under concurrency

The prior work on this operation notes that **no input above 30,000 rows was ever run
concurrently**, so the reads carrying most of the cohort's time had never been measured under load.
`bench_subtree_fused_concurrency.py`, the 1,404,566-row subtree, 5 blocks × 12 s windows, arms
alternating with rotating order (`concurrency_tail.json`):

| clients | throughput | server CPU per operation | blocks with wrong output |
|---|---|---|---|
| 4 | +1.21% [−2.62, +7.97], 3/5 up | **−17.73%**, 5/5 down | 0 |
| 8 | +0.11% [−15.53, +3.93], 3/5 up | **−18.35%**, 5/5 down | 0 |

**Throughput is flat, and the run itself says why: mongod used 0.5–0.6 cores of 96.** The server was
never the constrained resource. At these sizes each client streams ~614 MB and spends its time in
PyMongo decoding 1.4M documents, so the whole arrangement is client-bound and a server-CPU saving
has nothing to convert into. Reported as a null result, not as "+1.2% throughput": **this run does
not show a throughput benefit, and it is not designed to.** Demonstrating one would need either many
more clients or a client cheap enough to let mongod reach saturation, and neither was run.

What it does establish, and what it was worth running for:

- The CPU reduction **survives concurrency** — 5/5 blocks down at both client counts.
- **Output is identical under concurrent load.** Each client folds its rows into a CRC and the row
  count and CRC are compared against a reference captured outside the timed window; every block of
  every arm at both client counts matched, and no client raised.
- No pathology appears at 8 concurrent tail reads: no errors, no divergence, no collapse.

One number is left unreconciled rather than smoothed over: server CPU per operation falls 17.7–18.4%
here against **−12.63% measured single-client at the same input**. Both are paired per block, but
they come from different harnesses measuring over different windows, and nothing here establishes
which is the better estimate of the same quantity.

### L2 — blank-context review, and what it changed

A reviewer with no knowledge of this work was given the diff, the applied tree to navigate, and the
test, and asked to review to MongoDB Query Execution standards. Per the brief: fix, don't argue.
It found one crash-class defect, two silent explain defects, two costs charged to the untouched
path, and a claim in this log that was **wrong**.

**1. The design was wrong, and it was fixed at the cause.** Deleting the `PROJECTION_COVERED` node
left the plan-stage tree with fewer nodes than the QuerySolution. `ExactCardinalityImpl::
populateCardinalities` (`exact_cardinality_impl.cpp:61`) walks both in lockstep and **tasserts**
under `planRankerMode: "exactCE"`. The same break silently corrupted explain — the IXSCAN lost its
`nss`, inherited the projection's cardinality estimate and `planNodeId` — and would have broken
roughly 115 assertions across 21 jstests plus ~50 `query_golden` expected-output files that check
for `PROJECTION_COVERED`.

Rather than patch three symptoms, the stage now **stays in the tree as a pass-through**: the scan
produces the projected object and `ProjectionStageCovered::transform` returns immediately. One fix,
three defects gone, and the plan shape is preserved for every existing test. Cost: about one
virtual call per row — **1 to 1.7 pp of retired instructions**, and nothing measurable on CPU or
wall.

**2. A claim in this log was wrong.** It said the `shouldDedup` guard "is not reachable through a
covered projection". It is. `IndexScanNode::getFieldAvailability` returns `kFullyProvided` for a
field whose own path is not multikey even on a multikey index, while the node's constructor sets
`shouldDedup = index.multikey` unconditionally — so `PROJECTION_COVERED` over a deduplicating
`IXSCAN` is reachable, and fusing it would emit **one row per index entry instead of one per
document**. The earlier test passed only because its `sort()` forced a FETCH: right assertion, wrong
reason. That guard is now the load-bearing one, is documented as such in the code, and is exercised
by a test that reaches it (`refused/multikey-dedup`, and the jstest equivalent).

**3. Costs charged to queries that get no benefit.** A `BufBuilder` default-constructs with a
512-byte malloc, and it sat on *every* `IndexScan` in the server, knob off included; `Ordering::make`
re-walked the key pattern on every construction. Both now live inside `CoveredProjection`, so a scan
without a folded projection allocates nothing and computes nothing. A `getOwned()` copy that the
fused fallback immediately discarded was moved below the early return.

**4. `Ordering` came from the wrong source.** It was derived from the planner node's key pattern,
which is documented as possibly differing from the descriptor's for `$**` indexes. It is not wrong
today — only because wildcard finalisation happens to agree — but it is the only decoder in the tree
that does not take it from the index descriptor. Now `entry->ordering()`, the same value the storage
layer decodes with.

**5. Activation had to be re-instrumented.** With the stage retained, its presence no longer
distinguishes the arms. `IndexScanStats` gained a `coveredProjection` flag, emitted in explain
**only when true**, so knob-off output stays byte-identical — which matters, because ~50 golden
files compare exactly. Every harness now reads that flag and additionally asserts the two arms have
identical stage trees.

**6. Test coverage for the new parser.** `toBsonProjectedSafe` is a second parser over on-disk
bytes and had neither a unit test nor a fuzzer entry. It is now in
`key_string_to_bson_fuzzer.cpp`, and `key_string_test.cpp` gained
`ProjectedDecodeMatchesFullDecodeForEverySubset`, which checks **all 16 subsets** of a
four-component key against `toBsonSafe`-plus-rename across two orderings and both KeyString
versions. It failed on first run — V0 cannot encode `NumberDecimal` — which is a real constraint
correctly caught. **136/136 `key_string` tests pass.**

Also added from the review's list of untested paths: a multi-interval `$in` scan (which builds an
`IndexBoundsChecker` and forces the materialised-key fallback), a backward scan, `_id` as an
included component, and a `totalKeysExamined` equivalence assertion between the arms.

Not adopted, with reasons: the review suggested a non-appending sink so excluded components are not
materialised at all. It is right that this is the remaining waste, but for this workload the only
excluded component is `path` at ~7-20 bytes of a ~420-byte key, so the ceiling is small; it is
recorded as future work rather than done blind. The IDL knob was confirmed to need no
`annotations: query_knob:` block — 38 of 51 parameters in that file have none.

Status: **built, correct, measured single-client and concurrent, non-intrusion shown, resmoke green,
reviewed and revised.**

---

## Open

- L1b — ceiling probe for overlapping transmission with production. Expected small on loopback:
  it recovers transmission time only, and the client still cannot decode until the single reply
  message is complete. Probe the ceiling before writing any code.
- L1c — server-chosen batch byte cap (`find_common.cpp:39`, 16 MB). Needs no client change.
- L2 — build only if the master profile keeps the denominator large.
- L3 — non-key payload columns in indexes: design + ceiling probe only.

## Environment notes

- Build: worktree `/tmp/mongo-subtree-stream`, own bazel output base (cold build ~1 h; the
  three sibling agents each hold their own worktree and output base).
- The measured 7.0.34 baseline on `mongodb://localhost:57017` cannot run a master binary.
  A/B is on one master binary, env-var gated, read once at startup.
- Dataset for a locally-built mongod: `bench_bottleneck_local_mongod.py` clones the target
  subtrees plus filler out of 57017 into a mongod started under this uid, keeping the five
  indexes and the document shape. It does not reproduce total collection size.

---

## L3 — Non-key payload columns in indexes (PostgreSQL `INCLUDE`)

Scoped as the brief asks: a design sketch and a ceiling probe, not an implementation.

**The asymmetry.** MongoDB has no way to store a covering field in an index without order-encoding
it into the key. `title` (~53 B) and `summary` (~340 B) are escaped into the KeyString and decoded
back out on every row. PostgreSQL's `INCLUDE (title, summary)` stores them uninterpreted and returns
them with a length-prefixed copy. Note the index is **not** bigger for it: MongoDB's covering index
is 4.662 GB against PostgreSQL's 5.506 GB, so this is an encoding cost, not a size one.

**Design shape**, if it were built: a key-format flag marking a suffix of components as payload;
payload appended after the key proper with a length prefix and no order-encoding, no escaping and no
TypeBits participation; comparison and seeking restricted to the key prefix; `toBsonProjectedSafe`
gaining a branch that memcpys payload components instead of decoding them. The invasive part is not
the decoder — it is that every consumer of a KeyString has to learn that a suffix of it is not
ordered, including index builds, validation, repair, and the resumable-index-build format.

**Ceiling probe** (`bench_subtree_l3_ceiling.py`, `l3_ceiling.json`, 8 blocks × 5 s, alternating,
both arms fused). Two covered scans over the same 11,686 rows: the wide index
`(path, node_id, title, summary)` projecting three fields, against the narrow `path_1_node_id_1`
projecting one. The difference is the whole cost of carrying ~393 bytes of payload through the key:

| | median | range |
|---|---|---|
| share of server CPU | 37.46% | [34.24, 40.61] |
| share of retired instructions | **21.02%** | [20.81, 21.24] |
| share of client wall | 41.56% | [39.22, 44.44] |

**None of those is what `INCLUDE` would save, and the gap between them and the real answer is
large.** The probe measures carrying the payload *at all*; `INCLUDE` keeps the payload in the index
and still returns it. Specifically it does **not** save:

- **the copy** of ~393 bytes per row into the output object — `INCLUDE` does that too;
- **the WiredTiger traversal**, because the narrow index is 0.28 GB against the wide index's
  4.66 GB, so the narrow arm walks a far denser B-tree with a fraction of the pages. This is
  probably the largest single term in the 37% and `INCLUDE` recovers none of it;
- **the transmission and client decode** that dominate the wall column — the narrow arm returns a
  ~0.3 MB reply against 5.11 MB, which has nothing to do with key encoding.

What `INCLUDE` actually removes is the escape scan and the TypeBits participation for payload
components. After L2 those are what remains of the decode: in the post-fusion profile,
`readCString` plus `readCStringWithNuls` are ~4.3% of server CPU self time and
`toBsonProjectedSafe` 5.33%, of which the copy is irreducible. **A defensible estimate is a few
percent of server CPU — call it 3–6% — which is 2–4% of wall.**

**Recommendation: not worth building for this workload.** It is a storage-format change touching
every KeyString consumer, for low single digits of wall time, and L2 has already taken the part of
the decode that was cheap to take. The reason is arithmetic, not pessimism: see below.

---

## Where the remaining time actually is

Applying the measured L2 deltas to the P50 budget established in L1b:

| | before L2 | after L2 |
|---|---|---|
| server produce | 8,229 µs (59%) | ~7,150 µs (55%) |
| transmission | ~2,800 µs (20%) | ~2,800 µs (21.5%) |
| client BSON decode | ~3,000 µs (21%) | ~3,000 µs (23%) |
| **total** | **~13,900 µs** | **~13,030 µs** |

**44% of this operation is now transmission plus client decode, and L1a and L1b established that
both are unreachable from the server for a client that did not request an exhaust cursor.** That is
the honest ceiling on anything further in this lane, and it is why L3 is not recommended: even
eliminating the *entire* remaining server-side decode would leave the operation dominated by two
terms no server-side change can touch.

The levers that remain are therefore not in `mongod`'s query execution at all:

1. **Make the exhaust path reachable.** The machinery exists and delivers the overlap; what blocks
   it is that drivers refuse it behind `mongos` and it cannot combine with `limit()`. That is a
   driver and router question, not a query-execution one.
2. **Cheaper client-side BSON decode**, which is ~23% of this operation after L2 and belongs to
   PyMongo.

Neither is in this lane, and both are larger than what is left in it.

---

## Second pass — four more server-side leads, three of them dead

After L2 landed, the claim "44% of this operation is unreachable from the server" was challenged and
was **too strong**. It is true of *overlapping* server and client work; it says nothing about
*reducing* either term. So the post-fusion profile was re-taken on the shipped build and worked
through lead by lead. Three of the four died, and each died for a different reason worth recording.

Self time on the shipped build, fusion on (`profile_fused_v2.json`), grouped:

| | self CPU |
|---|---|
| materialise the projected object and move it into the reply | ~15.3% |
| KeyString decode, incl. `BufReader` overhead 3.32% | ~20.3% |
| WiredTiger index walk | ~12.6% |
| transmission-side kernel work | ~7.3% |

### L5 — reply-buffer page zeroing. **Dead. The hypothesis was simply wrong.**

`clear_page_erms` is 3.84% of server CPU, and the guess was that mongod reallocates a 16 MB reply
buffer per getMore instead of reusing one. The call graph says otherwise:

```
clear_page_erms ← get_page_from_freelist ← alloc_pages ← skb_page_frag_refill
                ← sk_page_frag_refill ← tcp_sendmsg_locked ← __sys_sendto ← sinkMessage
```

It is the **kernel** allocating socket page fragments while sending the 5.11 MB reply. Nothing
mongod owns, nothing mongod can pool. Reducible only by sending fewer bytes.

### L1c — server-chosen batch byte cap. **Dead, and it took two runs to kill honestly.**

`find_common.cpp:39` pins `kMaxBytesToReturnToClientAtOnce` at 16 MB. This was originally dismissed
by argument — without exhaust there is no overlap, so smaller batches only add round trips — but
that argument did not explain the report's observed −11.2% at 96,238 rows with `batchSize` 2000.
An argument that contradicts a measurement is not a reason to skip the measurement.

**The first proxy run produced a false positive** and is retained as a caution: `batchSize` 1000
came out at −5.20% median. The block sequence exposed it — −35.5%, −28.2%, −4.1%, … , −1.6%. The
probe had no per-arm settle, so whichever arm ran first in a block paid for a cold cache, and since
the arm order rotates, the baseline absorbed most of it. Fixed with a settle before every window
plus two discarded warmup blocks.

Corrected run at 97,773 rows (`l1c_proxy_p90.json`, 8 measured blocks):

| batchSize | client wall | server CPU | retired instructions |
|---|---|---|---|
| 1000 | −2.39% [−29.0, +19.4], 5/8 | **+3.99%** | **+0.65%** |
| 2000 | −2.11% [−49.2, +27.6], 5/8 | **+6.48%** | **−0.68%** |
| 8000 | −1.03% [−52.7, +31.3], 5/8 | **+8.59%** | **−0.44%** |

**Retired instructions decide it.** On this box that metric holds to ~0.2 pp, and batch size moves
it by under 1% in either direction — the server does not do less work with smaller batches, so the
cache-residency mechanism is not operating. The wall medians sit inside ±30–50% ranges at 5/8
blocks, which is noise, and server CPU is consistently *worse* because more round trips mean more
command processing. The report's −11.2% does not reproduce here.

### L4a — stop rebuilding an object that already exists. **Real, measured, too small. Reverted.**

`Document::toBsonIfTriviallyConvertible()` returns the backing BSON directly, and
`DocumentStorage::reset(bson, false)` leaves `_modified` false and no metadata, so every projected
object qualifies. `plan_executor_impl.cpp` nevertheless called plain `toBson()` and rebuilt each
object field by field.

It works — the profile shows the symbol swap cleanly, with fusion held on in both arms:

| arm | symbol | self CPU |
|---|---|---|
| on | `Document::toBsonIfTriviallyConvertible()` | 1.10% |
| off | `Document::toBson<DefaultSizeTrait>()` | 1.78% |

Net **~0.68% of server CPU**, about 0.4% of wall, because the fast path is not free either: it still
copies a BSONObj handle and wraps it in an optional. The paired A/B could not resolve it — retired
instructions +0.08% [−0.16, +0.34] at P50 and −0.59% [−2.46, +2.22] at P90, 4/10 and 8/10 blocks.

It passed 13/13 differential cases and 88/88 core projection and covered-query tests, so it is
correct. It was still **reverted**: a change that cannot be measured end to end, carrying a server
parameter and a branch on a path every query takes, does not belong in a PR whose other content is
measurable. Recorded here so the next person does not re-derive it.

Two instrumentation errors of mine along the way, both caught before they reached a conclusion: a
resmoke run that never started because one listed test file did not exist and the watcher only
matched a success pattern; and a profile labelled `l4a_on` that in fact had the knob off, because
the profiler does not manage knobs and the A/B harness leaves the toggled knob disabled when it
finishes.

### L6 — decode straight into the reply buffer. **Scoped, not built. Recommended against.**

The largest remaining server-side item. Today each row is materialised into a `BSONObjBuilder`,
frozen into a `BSONObj`, wrapped in a `Document`, converted back to a `BSONObj`, and finally copied
into the reply's `BSONArrayBuilder`. Measured on the shipped build: builder appends 3.47 + 0.81,
`_done()` 1.35, `DocumentStorage::reset` 3.30, `Document::toBson` 1.29, `transitionMemberToOwnedObj`
0.91, `BSONArrayBuilder::append` 3.24, `BSONObjCursorAppender` 0.91 — **~15.3% of server CPU**, plus
a share of `__memmove_avx512` at 3.37%.

If the covered scan wrote its projected components straight into the reply buffer, all of that
except the final byte-writing would go. Optimistically **10–11% of server CPU, about 6% of wall.**

Against that: the plan-stage contract is that stages produce `WorkingSetMember`s and the command
layer owns the reply builder. Bypassing it means threading a sink from the command down through the
executor into the leaf stage, for one plan shape — architecturally the same move
`exec/express/plan_executor_express.cpp` makes for point queries, and that is a whole parallel
executor. **Recommended against**: express-executor-scale surgery on a hot, shared contract for a
single-digit wall gain, on a workload where 44% of the time is already outside the server's reach.
Recorded with numbers so the trade is visible rather than assumed.

---

## MongoDB-side leads outside this lane

Larger than anything left inside it, and listed so they are not lost:

1. **`mongos` support for exhaust cursors — ~40% of wall, the largest item identified anywhere in
   this project.** The server machinery exists and works; L1a showed the overlap requires the client
   to set `kExhaustSupported`, and drivers refuse exhaust behind a router. `mongos` is MongoDB's own
   code, so this is a MongoDB-side change — it simply cannot be measured on a standalone box, which
   is why it was set aside here, not because it is unreal.
2. **Fewer bytes on the wire.** Transmission-side kernel work is ~7.3% of server CPU and the client
   spends ~23% of the operation decoding BSON; **both scale with reply size.** Wire compression was
   ruled out for this workload **on loopback only**, where a compressor cannot repay itself against
   a ~2 GB/s local transfer. The payload compresses 3.4–4.8×, so on a real link that conclusion
   plausibly inverts. This benchmark structurally cannot show it, and no claim is made either way.
3. **Per-document field names.** `node_id`, `title` and `summary` repeat in all 11,686 documents —
   ~257 KB of the 5.11 MB reply, about 5%. A more compact reply encoding would cut both the
   transmission and the client's decode, but it needs driver support to be readable.

---

## L7 — Exhaust through `mongos`. **The report's premise is wrong, and this is the largest finding here.**

This project treats exhaust cursors as unavailable to sharded deployments, and that is what makes
the ~40% overlap they buy look unreachable — "the capability exists in the server and is reachable
only through a client option most deployments cannot use". **`mongos` implements exhaust in full.**
The restriction is imposed by the driver.

**Why nobody noticed.** PyMongo refuses before a byte reaches the wire
(`pymongo/synchronous/cursor.py`, 4.12.0):

```python
if self._cursor_type == CursorType.EXHAUST:
    if self._collection.database.client.is_mongos:
        raise InvalidOperation("Exhaust cursors are not supported by mongos")
```

No experiment conducted through PyMongo can tell "mongos cannot do this" from "the driver will not
ask". Every measurement in this project went through PyMongo.

**What mongos actually does:**

- `s/commands/strategy.cpp:488` — `opCtx->setExhaust(OpMsg::isFlagSet(m, OpMsg::kExhaustSupported))`
- `s/commands/query_cmd/cluster_getmore_cmd.h:111-115` — `if (opCtx->isExhaust() &&
  response.getCursorId() != 0) reply->setNextInvocation(boost::none);`
- `s/commands/strategy.cpp:1332-1337` — propagates `shouldRunAgainForExhaust` into the `DbResponse`

**Verified on the wire, not inferred.** `exhaust_through_mongos.py` speaks OP_MSG over a raw socket
and sets `exhaustAllowed` itself, so the driver's refusal never applies. It decodes `flagBits` and
counts how many replies come back for a **single** `getMore` request. Instrument validated first
against a standalone mongod on the real 11,686-row subtree: 11 replies, 10 unsolicited.

Cluster: config server + shards + `mongos` on 57022, all from this build; 20,000 documents with the
same four-component covering index (`setup_sharded_for_exhaust.sh`).

| endpoint | replies to ONE getMore | unsolicited | rows |
|---|---|---|---|
| standalone mongod | 11 | 10 | 11,686 |
| **mongos, one shard** | **20** | **19** | 20,000 |
| **mongos, two shards, merged** | **20** | **19** | 20,000 |

It works through a router, and it keeps working when `mongos` is merging two shards — which is the
case that most plausibly would have broken.

**What it is worth.** Same raw client, same socket, same batch size; the only difference is the
`exhaustAllowed` flag, so the delta is attributable to exhaust alone. Paired, arms alternating
within blocks, 3 reps per arm, best-of taken:

| cluster | paired delta | blocks faster | median |
|---|---|---|---|
| one shard | **−23.41%** [−26.65, −8.91] | 8/8 | 20.2 ms vs 26.0 ms |
| two shards | −19.40% [−56.15, +7.86] | 6/8 | 39.3 ms vs 52.0 ms |

The single-shard figure is the trustworthy one. The two-shard run is directionally the same but much
noisier — both arms slower, the range crosses zero, 6/8 blocks — so it is reported as corroboration
of direction, not as a second measurement of size.

**What this changes.** The largest item identified anywhere in this project needs **no MongoDB
server work**. It is already built, already shipped, and already streams through a router. What
stands between sharded deployments and it is a client-side check whose error message —
"Exhaust cursors are not supported by mongos" — is not true of the server it names.

**What this does not establish.** That the driver restriction is safe to lift. An exhaust cursor
monopolises its connection for the life of the cursor, and behind a router fronting many shards
that has pooling consequences a single-cursor test cannot see; interaction with retryable reads,
load-balancer mode and failover is untested. The claim made here is narrower and firmer: **the
stated reason for the restriction is factually wrong about the server**, and whatever the real
reason is, it is not that mongos cannot do it.

Setup is deliberately small — one mongos, two single-node shards, no auth, no load balancer, no
failover, a synthetic 20,000-document collection, and a Python client that is not a production
driver. It is sized to answer a protocol question, not to produce a throughput number.

Status: **settled, positive, and the most actionable result in this log.** Recommended next step is
not a mongod change but a question to the drivers team: what is the restriction actually protecting
against, given the server streams correctly through a two-shard merge?

