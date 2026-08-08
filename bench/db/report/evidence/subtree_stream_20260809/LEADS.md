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

Status: **built, correct on the target shape, measurement in progress.**

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
