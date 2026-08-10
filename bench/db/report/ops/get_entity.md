# `get_entity` — fetch one entity by `_id`

**What MongoDB should change to serve this shape faster.** Everything below is scoped to changes in
`mongod` or in PyMongo. Application-side workarounds appear only in §6.

**MongoDB 0.142 / 0.166 ms against PostgreSQL 0.074 / 0.082 ms (P50 / P95), 1.9×.**

The query is an **unhinted** `find_one` on `_id` in `layout_shared_text` (9,000,301 documents, sole
index `_id_`), which takes `IDHACK` — confirmed by live `explain`: `PROJECTION_SIMPLE → IDHACK`,
`explainVersion` 1. PostgreSQL runs the equivalent primary-key lookup against
`layout_shared_pg_text_pkey`.

**Why this operation matters most to MongoDB's decision-making, despite being the smallest gap.** It
is the only one of the four already on MongoDB's fastest available plan. Every server-side change
proposed for `get_node` and `get_children` works by moving those operations toward what `get_entity`
already is — so **`get_entity`'s residual gap is the ceiling on all of them**. If this operation is
still 1.9× PostgreSQL with planning already skipped, then no amount of planner or fast-path work will
close the tree workload's gap, and the remaining effort belongs in the command path and the driver.

Note the careful wording: on the fastest available *plan* is not the same as *at the floor*. §5 shows
it is 2.1× the floor.

Provenance: server figures are CPU, client figures are wall, never subtracted from one another. All
MongoDB figures are from the logging-off arm (`bottleneck_20260806/mongo_cpu_arms_nolog.json`).

**One known bias in MongoDB's favour, in every server-CPU figure here.** The instrument counts only
threads whose name begins with `conn`. In the same artifact, `get_entity_hit` reports
`conn_cpu_us_per_op` 45.321 against `total_cpu_us_per_op` 48.653, with `Service.Fixed-0` alone
contributing 2.65 µs/op against an idle baseline of 0.66. PostgreSQL's contract is the CPU burned by
the backend *process*. So roughly 2.6 µs/op — 5.8% — of load-proportional server CPU is excluded on
the MongoDB side only, and every MongoDB-versus-PostgreSQL ratio below is that much too kind to
MongoDB. The version comparison in §2c uses the same filter, so it is blind to any work either
version does off the connection thread.

---

## 1. Where the gap is

| | MongoDB 7.0.34 | MongoDB master | PostgreSQL prepared | PostgreSQL unprepared |
|---|---|---|---|---|
| server CPU | 45.4 µs | **38.1 µs** | 19.5 µs | 30.0 µs |

The master column is measured, not projected — §2c. It closes **46% of the gap against the matched
unprepared PostgreSQL arm**, and it is the single largest server-side movement anything in this
document reports. Everything else in §1 and §2 describes 7.0.34, which is what the instance holding
the dataset runs.

The ratio is **2.33× against the prepared arm and 1.51× against the unprepared arm**, against a
headline wall ratio of 1.9×. Which arm is right is answered in §5; quoting "about 2×" without naming
the arm is not legitimate.

PostgreSQL's own per-phase CPU when it plans (`pg_cpu_arms.json`,
`phase_split.pg_get_entity__unprepared` medians — its internal split only, that instrument running
over a container-local socket): PARSER 2, PARSE ANALYSIS 2, REWRITER 1, **PLANNER 7**, EXECUTOR 3 µs.
Planning is the largest of the five phases, 47% of their sum, and its prepared arm saves 10.5 µs
(30.0 → 19.5, 35%).

MongoDB's client-side term was never measured in the matched artifacts, so §1 carries no MongoDB
client cell.

## 2. Where MongoDB's 45.4 µs goes

`perf_nolog/get_entity_hit.perf.data` — 194 MB, 83,543 samples, 92.58% of self frames resolved, same
instrument and basis as `get_node`'s.

From the **inclusive** profile at 45.354 µs/op. A caller and its callee are both counted, so **these
must not be added together** — the artifact's own header says so. The *exclusive* partition, which
may be added, is §2a.

| frame | inclusive % | µs |
|---|---|---|
| `sinkMessage` — send reply | 22.25 | 10.09 |
| `sourceMessage` — receive request | 9.07 | 4.11 |
| `AutoGetCollectionForReadCommandMaybeLockFree` ctor | 7.99 | 3.62 |
| **`IDHackStage::doWork` — the entire point lookup** | **7.67** | **3.48** |
| `getExecutorFind` | 4.78 | 2.17 |
| `parsed_find_command::parse` | 4.38 | 1.99 |
| `AutoGetCollectionForReadCommandMaybeLockFree` dtor | 2.04 | 0.93 |
| `projection_ast::parseAndAnalyze` | 2.00 | 0.91 |

**The actual lookup is 3.48 µs; sending the reply is 10.09.** The shape is the same as `get_node`'s —
transport dominates, then collection acquisition, then execution — but the shared components are
**not** paid at equal magnitude: collection acquisition is 3.62 µs here against `get_node`'s 5.62,
command parse 1.99 against 2.43. `get_entity` is not simply `get_node` minus planning.

## 2a. The exclusive partition, and the target list it produces

**It exists.** `runs/bottleneck_20260806/decomp_get_node/get_entity_phases.txt`, produced by the
same `phases.py` leaf-to-root classifier that produced `get_node_phases.txt`, over the same 83,543
samples, summing to 100.000% / 45.354 µs. Earlier versions of this file and of the hand-off said the
pass had never been run; that was wrong, and the two proposals below follow from it.

Two of its rows reproduce the independent inclusive pass exactly — projection `parseAndAnalyze`
1.998% and release 2.044% — which is what says both ran over the same sample set. The third does
not: collection acquisition is 8.027% exclusive against **7.985%** inclusive, and an exclusive
figure cannot exceed an inclusive one for the same frame. An earlier version of this paragraph
quoted the inclusive to two decimals ("7.99") so that a 0.042-point disagreement read as rounding.

By group, exclusive, µs of 45.354:

| group | µs | % | |
|---|---|---|---|
| `tx` transport | 17.954 | 39.6 | send reply 10.163, receive request 4.282, `~OperationContext` 1.666, acceptResponse 0.953 |
| `cmd` dispatch | 9.334 | 20.6 | continuation machinery 4.158, `_initiateCommand` 1.947, `makeOperationContext` 1.191 |
| `acq` collection | 4.568 | 10.1 | acquire + WT snapshot 3.641, release + rollback 0.927 |
| `exec` execution | 3.867 | 8.5 | rest of `getNext` 2.854, record fetch 0.860, projection transform 0.152 |
| `plan` planning | 2.164 | 4.8 | see M4 |
| `parse` | 1.802 | 4.0 | |
| `proj` projection analysis | 1.511 | 3.3 | `parseAndAnalyze` 0.906, `optimize` 0.506 |
| `find` `FindCmd::run` | 1.240 | 2.7 | |
| `post` | 1.055 | 2.3 | index-usage stats 0.594, `markKillOnClientDisconnect` 0.210 |
| `filt` match expression | 0.797 | 1.8 | `MatchExpressionParser::parse` 0.761 |
| `teardown` | 0.601 | 1.3 | |
| unattributed | 0.460 | 1.0 | |

**Transport and dispatch are 60.2% of the operation. All query work together — execution, planning,
parsing, projection analysis, match expression — is 10.14 µs, 22%.** That is the shape of a command
whose query is already trivial, and it is why §5's conclusion holds.

## 2b. Whose code is it — the origin split

The phase table above says *which subsystem* a sample is in. It does not say whether the sample is
MongoDB's own code, the kernel, or this box's endpoint-protection stack, and only the first is
something MongoDB can change. `bench/db/decomp_origin.py` classifies the **leaf** frame of every
sample, so the parts sum to the phase and the phases sum to the operation.

Whole operation, 83,543 samples, 45.354 µs:

| origin | µs | % |
|---|---|---|
| `mongod` — resolved, non-WiredTiger, non-allocator | 21.761 | 48.0 |
| kernel | 10.803 | 23.8 |
| endpoint protection + container networking | 3.994 | 8.8 |
| unresolved | 3.365 | 7.4 |
| WiredTiger | 3.288 | 7.3 |
| allocator | 2.142 | 4.7 |

**MongoDB's own code (mongod + WiredTiger + allocator) is 27.19 µs, 60% of the operation** — in
*this* capture. The host-process capture in §2c.1 puts the same quantity at 71.8%, and the two are
not reconciled by anything below. The difference is symbol resolution, not code: perf cannot resolve
glibc inside the container, so glibc leaves fall into `unres` here (3.365 µs, 7.4%) and into the
`mongod` bucket there. Treat 60% as a floor and 72% as a ceiling on the same quantity.

But it is not where the phase table's biggest number is. Splitting the two transport phases:

| phase | µs | mongod | kernel | EDR |
|---|---|---|---|---|
| `_sendResponse` — send reply | 10.163 | **0.210 (2.1%)** | 6.357 | 3.354 |
| `_getNextWork` — receive request | 4.282 | 0.513 (12.0%) | 3.226 | 0.368 |

**Of the 14.4 µs in those two phases, 0.72 µs is MongoDB's code.** Two qualifications the earlier
version of this paragraph did not make. The `tx` group is **17.954 µs**, not 14.4 — its other
members (`~OperationContext`, `acceptResponse`, `scheduleIteration`, `doOneIteration`) contribute
about 3.0 µs more of MongoDB's own code. And "not MongoDB's code" is not "not MongoDB's to
optimise": the syscall count, the framing and the copies are MongoDB's choices and they drive the
kernel term. What the split does support is narrower — **most of the transport phase is not code a
patch to mongod could delete.**

**Where MongoDB's 27.19 µs actually sits.** It is flat: the largest single mongod leaf frame is
`operator new` at 1.106 µs, 2.4%. Grouped:

| | µs | % | |
|---|---|---|---|
| allocator | 2.016 | 4.4 | `operator new` 1.106, `operator delete` 0.787, `tc_malloc` 0.123 |
| refcounting | 0.905 | 2.0 | `_Sp_counted_base::_M_release` 0.432, `_M_add_ref_copy` 0.197, `intrusive_ptr_release` 0.276 |
| BSON re-walking | 0.880 | 1.9 | `BSONElement::computeSize` 0.350, `BSONObj::getField` 0.274, builder 0.256 |
| WT config parsing | 0.681 | 1.5 | see M6 |
| WT row search | 0.468 | 1.0 | `__wt_row_search` 0.376, `__wt_row_leaf_key` 0.092 |

**There is no ten-microsecond item.** That is the finding, and it is the same shape as the driver
side: per-command cost spread thin across machinery that each does a little.

### M6 — `begin_transaction` re-parses a config string on every read · **already fixed on master**

`__config_next` is 0.588 µs/op, 1.3% of server CPU, and 91% of it comes from
`WiredTigerBeginTxnBlock` → `__session_begin_transaction`.

The cause is exact. For a plain read all three branches in
`wiredtiger_begin_transaction_block.cpp:93-113` are false, so the config string is **empty** — yet
7.0.34 still builds a `str::stream`, converts it to a `std::string`, and passes `""`. WiredTiger's
`__wt_config_gets_def` carries its own comment that parsing config strings is "expensive" and has a
fast path for it, but that path is selected by the *length of the cfg array*: `nullptr` gives length
1 and returns the default immediately, while `""` gives length 2 and sends **every key lookup**
through `__wt_config_getones` to parse the empty string.

**Master already fixes this properly**, and by more than the one-line `nullptr`: it pre-compiles
every non-default combination through WiredTiger's compiled-configuration API at startup, skips the
all-default case outright (`if (config == 0) continue`), and passes a compiled token rather than a
string. So this is not a proposal — it is a measurement of what a 7.0 deployment still pays and what
upgrading recovers: **0.59 µs per read, on every read the server serves.**

**Environment note, corrected.** 8.39% of self samples land in kernel modules inside `mongod`'s
connection thread, and an earlier version of this note called all of them "a property of this box".
Only about half are: `dsa_filter`, `bmhook` and `tmhook` (1.776 µs of the send-reply phase) are
endpoint protection. `nf_tables`, `nf_conntrack`, `nf_nat` and `bridge` (1.310 µs) are the standard
Docker and Kubernetes networking path and are present on very ordinary MongoDB deployments;
`decomp_origin.py` merges the two categories into one bucket. The runbook records that
`nft_do_chain` fires on host loopback as well, so this is not container-only either. 8.39% is also
a floor rather than the total: `tmhook` appears on 29.5% of chains *inclusive*, so the transport
inflation is larger than a leaf split can bound. It is common-mode with the PostgreSQL psycopg arm.

## 2c. What master has already recovered

Everything above is 7.0.34, because that is what the instance holding the dataset runs. M6 showed
master had already closed one of the items the profile found, which raises the question the rest of
this section answers: **how much of the 45.4 µs has MongoDB already recovered since 7.0?**

Measured, not inferred. `bench/db/bench_entity_version_ab.py`, both servers as host processes on
loopback so neither pays the container's published port or its netfilter modules, both pinned to one
NUMA node, both serving the same 9,000,000-document collection generated by
`bench/db/load_entity_dataset.py`, alternating within blocks. master is a clean build of the pinned
base `0561c098` — the fork's own tree carries another agent's uncommitted express work and cannot be
used as a baseline.

| | 7.0.34 | master (9.0.0-alpha0) | paired delta |
|---|---|---|---|
| server CPU | **45.00 µs** | **38.11 µs** | **6.74 µs, 15.0%, 14/14 blocks** |
| wall | 115.8 µs | 105.4 µs | 10.4 µs, 9.0% |

Spread on the paired delta [+5.08, +11.34]; **every block favours master**. Three runs converge:
6.65 µs unpinned, 6.24 µs pinned under load, 6.74 µs pinned and quiet.

The plans, printed by the harness rather than asserted:

- 7.0.34 `PROJECTION_SIMPLE → IDHACK`, two stages
- master `EXPRESS_IXSCAN`, with the projection folded into the express stage and no separate
  projection stage (`projectionCovered: false`, since `text` is not in the index)

**Against the unprepared PostgreSQL arm at 30.0 µs**: 7.0.34 is 15.0 µs above it, master 8.1 µs, so
master has closed **46%** of that particular gap. Four things have to be said with that number, and
an earlier version of this paragraph said none of them:

- **Against the *prepared* arm at 19.5 µs the same arithmetic gives 27%.** This document opens with
  1.9×, which §1 identifies as the *prepared* ratio — so "the gap this document opened with" is the
  27% one, not the 46% one.
- **The unprepared arm is the generous choice here.** §5 justifies it with "MongoDB re-plans on
  every call", which is an argument about `get_node`. On `get_entity` §2a puts planning at 2.164 µs
  of 45.354, and §1 and §5 both say planning is already skipped. Unprepared PostgreSQL pays about
  12 µs of parse, rewrite and plan against MongoDB's 6.27 µs.
- **The transports differ.** 45.00 and 38.11 are host processes on loopback; 30.0 is the psycopg arm
  through a container-local socket.
- **PostgreSQL's number is quantised.** Its CPU comes in 10 ms clock ticks — the two unprepared
  repeats are 31.5 and 30.0 µs/op. ±1.5 µs on that one figure moves the closure between about 42%
  and 51%.

Three things to hold against this figure:

- The generated collection compresses about 16% better than the real one (2,745 MB against 3,269).
- Both arms are host processes, so this is not the same transport as the containerised 45.354 µs.
  The two nonetheless landed within 0.4 µs of each other, which is either a coincidence or evidence
  that the EDR modules the origin split found also fire on host loopback; it was not chased down.
- The measurement is `get_entity` only. Express is the `get_node` agent's lane, and nothing here
  modifies it — this measures stock behaviour on both sides.

The gate for all of it was a null test with **both arms pointed at the same server**, which must
report a delta of zero. The first attempt did not: drawing fresh random ids per block, against 9 GB
of uncompressed documents in an 8 GB cache, meant whichever arm ran first paid the read and warmed
the ids for the second — 90-117 µs against 44, flipping with the block parity. With a warmed fixed
working set the null test reports **+0.00 µs**, and only then was the comparison run.

### 2c.1 Where master's 6.74 µs went, and what it left behind

Both versions profiled the same way — host processes, same collection, same 40,000-id working set,
both driven for 60 seconds before perf attached, both captured at load average under 3. Scaled by
the server CPU each was measured at. `bench/db/compare_version_profiles.py`, evidence in
`report/evidence/entity_master_20260809/`.

By origin (µs of the 45.00 and 38.11):

| origin | 7.0.34 | master | delta |
|---|---|---|---|
| `mongod` | 23.865 | 18.392 | −5.474 |
| WiredTiger | 6.322 | 4.842 | −1.480 |
| allocator | 2.132 | 2.882 | **+0.749** |
| **MongoDB's own code** | **32.320** | **26.116** | **−6.204** |
| kernel | 10.043 | 9.427 | −0.616 |
| EDR / netfilter | 2.637 | 2.567 | −0.070 |

**The whole saving is MongoDB's own code** — that is what this table shows and it is worth saying.
What it does not do is *demonstrate* the 6.74 µs: `compare_version_profiles.py` scales each profile
by the µs/op the A/B measured, so the 6.89 µs difference is imported from that run and apportioned
here, not re-derived. The two captures have near-identical sample counts (108,467 and 108,417), i.e.
the same CPU-seconds. For the same reason an earlier version of this paragraph had the transport
argument backwards: on an identical transport the kernel and netfilter *absolute* terms should move
by about zero, and they move by 0.69 µs only because of the scale factor. By share they rise, from
28.2% to 30.2%.

**Below the origin table, leaf-frame attribution mostly does not survive.** The two binaries are
built differently — 7.0.34 is MongoDB's release build out of the `mongo:7` image, master is a local
`bazel --config=opt` build — so a symbol can move without any work moving. An earlier version of
this section reported two "regressions" that were exactly that, and the corrections are worth more
than the claims were:

- **`OperationContext` decorations were reported as +0.608 µs, "absent from 7.0.34 entirely". That
  was wrong.** 7.0.34 carries **0.604 µs** of the same work, spread across 84 templated
  `DecorationRegistry<...>::constructAt/destroyAt` symbols, none larger than 0.026 µs; master
  consolidates it into 17 symbols. Grouped, it is 0.604 → 0.644, **+0.04 µs**. The claim is
  withdrawn.
- **"document copy 1.432 → 0.963, −0.469" was wrong in both label and sign.** The group was
  `__memmove` plus glibc `__strlen`. `__memmove_avx512_unaligned_erms` — the only frame that copies
  the document — goes 0.781 → **0.932**, i.e. up 0.151. The saving was all `__strlen_evex`
  (0.651 → 0.031), which is not document copying.

What survives grouping, each checked by summing every symbol matching the family rather than the one
symbol that happened to be largest:

| | 7.0.34 | master | delta |
|---|---|---|---|
| reference counting (`shared_ptr` + `intrusive_ptr`) | 1.431 | 0.428 | −1.003 |
| WiredTiger config parsing | 0.793 | 0.043 | −0.750 |
| glibc `strlen` | 0.651 | 0.031 | −0.620 |
| allocator | 2.132 | 2.882 | +0.749 |
| `__memmove` | 0.781 | 0.932 | +0.151 |
| `OperationContext` decorations | 0.604 | 0.644 | +0.040 |

**These six net to −1.433 µs against the −6.204 the origin table decomposes: they account for 23% of
the saving, and the other 77% is a long tail.** This is not a decomposition of where master's saving
went, and it should not be read as one. Two things in it are worth saying on their own:

- **M6 is confirmed by measurement rather than by reading the source**: WiredTiger config parsing
  falls from 0.793 µs to 0.043, which is what the compiled-configuration change in §2 predicted.
- **The allocator got 0.749 µs dearer on master.** That figure is the `alloc` origin bucket, which
  is a classifier over every allocator symbol and so is not vulnerable to the renaming above. It is
  the one "master made this worse" claim here that survives scrutiny.

**A target list for master, with that caveat attached.** The largest MongoDB-attributable groups on
master are WiredTiger's row lookup at 3.40 µs — the actual index traversal, and not obviously
removable — and the allocator at 2.88 µs. Nothing on master is worth more than about 3.4 µs, so the
shape of the conclusion is unchanged from 7.0.34 even though the individual items are not the same.

**One capture was thrown away to get here.** The first pair had master long-running and 7.0.34
freshly loaded, and 7.0.34's profile carried 2.211 µs of snappy decompression against master's
*exactly* zero — and exactly zero is not a shape a version difference produces. Re-captured with
both driven 60 seconds before attaching, decompression is 0.000 on both sides and the
MongoDB-code delta is unchanged at −6.204 µs against −6.178. The artifact moved where the time was
attributed inside `mongod` without changing the total, which is the reassuring outcome.

## 2d. The same-transport grid — where the gap actually is

Every cross-engine figure above compares a containerised mongod behind a docker published port
against psycopg on a container-local socket — two different transports. This section removes that:
**both engines as host processes on this box, each measured over TCP loopback and over its unix
socket, serving the same 9,000,000 value-checked rows** (`bench_entity_cross_engine.py`,
`runs/entity_driver_20260809/cross_engine_20260810.json`; mongod is the clean master build, PG is
16 with `shared_buffers=8GB` on tmpfs; one held connection per arm, 40,000-id warmed working set,
arms rotated within 10 blocks, load < 4).

| arm | server CPU µs | wall µs | floor CPU (`ping`/`SELECT 1`) | above floor |
|---|---|---|---|---|
| mongo master · TCP | 39.49 | 131.2 | 17.72 | 21.78 |
| mongo master · unix | 35.43 | 121.2 | 16.12 | 19.31 |
| PG16 unprepared · TCP | 32.60 | 60.2 | 15.62 | 16.98 |
| PG16 prepared · TCP | 21.62 | 49.0 | 14.07 | 7.54 |
| PG16 unprepared · unix | 28.68 | 53.5 | 12.49 | 16.19 |
| PG16 prepared · unix | 17.65 | 42.4 | 11.04 | 6.61 |

Client CPU on the same rig (system Python 3.10, stock pymongo 4.13.1 with C extensions, psycopg 3
binary; 6 blocks × 2000):

| client | client CPU µs | wall µs |
|---|---|---|
| pymongo `find_one` | 103.5 | 139.2 |
| psycopg unprepared | 26.7 | 61.2 |
| psycopg prepared | 28.1 | 51.4 |

**Three conclusions this grid forces, and two of them correct this document.**

1. **At equal transport the server-CPU gap is small.** Master against unprepared PG is
   39.5 vs 32.6 over TCP and 35.4 vs 28.7 over unix — **1.21×–1.24×**, about 6.9 µs, of which the
   above-floor difference is 4.8 µs. The 1.51× that §1 reports for the unprepared arm was partly the
   two transports. The floors themselves are 2.1–3.6 µs apart, and master's `ping` floor (17.7) is
   well under 7.0.34's 21.5.

2. **Against *prepared* PG the server gap is structural and real**: above floor, 21.8 vs 7.5 —
   MongoDB's express path still parses and dispatches the full command every time, and the wire
   protocol has no prepared-statement equivalent. That ~14 µs is the part of the server story that
   survives every correction in this document.

3. **The end-to-end gap is the driver, almost exactly.** Same transport, unprepared: wall differs by
   **78.0 µs** and client CPU by **76.8 µs**. The famous wall ratio on this operation is, at equal
   transport, ~98% PyMongo-vs-psycopg and ~2% mongod-vs-postgres. This corrects §3 of
   `get_entity_driver.md`, which ranked the server upgrade above the driver work: **on wall, at
   equal transport, the driver is the biggest lever on this operation and it is not close.**

Where the levers stand on this rig, measured (mongo TCP arms):

| configuration | wall µs |
|---|---|
| pymongo 4.13 stock | 139.2 |
| driver master, cursor path forced (same process) | 115.1 |
| driver master + the `find_one` fast path (PR #1) | **98.9** |
| psycopg unprepared | 61.2 |
| hand-written OP_MSG client floor (§5, ~17 µs client CPU) implies | ~72 |

So driver master plus PR #1 removes ~40 µs of the 78 µs driver gap today; the remaining ~27 µs of
client-CPU excess (62.3 vs 26.7) is the ceiling-limited machinery `get_entity_driver.md` §3
describes, and a minimal client demonstrates most of it is mechanically removable at the cost of
the driver's feature set. Coalescing (§6) remains the only lever that crosses the whole gap and
reverses it.

Caveats, stated rather than buried: the grid's client is Python 3.10 with stock 4.13.1, the
patched-driver row is Python 3.14 from this project's build — compare within a table, not across;
PG's 10.5 GB table exceeds its 8 GB `shared_buffers` but sits on tmpfs, so misses are page-cache
reads for both engines; per-block spread is in the JSON (mongo_tcp CPU ranged 39.2–43.8); and the
server-CPU counter carries the conn-thread-only bias noted at the top of this file — PG's backend
processes are counted whole, mongod's non-`conn` threads are not.

## 3. Two version traps

- **`get_entity` does not use the `EXPRESS` executor.** Express is 8.0+ and the measured server is
  stock **7.0.34**: no `ExpressPlan` or `express_plan` anywhere in `/home/junyao/code/mongo-r7.0.34`,
  while master has `src/mongo/db/exec/express/express_plan.cpp`. It uses `IDHACK`. An earlier hand-off
  in this project asserted express here and the error propagated through two sessions.
- **`internalQueryPlannerUseMultiplannerForSingleSolutions` does not exist in 7.0.34** — master-only,
  at `query_optimization_knobs.idl:680`. Whether it exists on 8.x is not verifiable here.

---

## 4. What MongoDB should change

The list is short, and that is the finding: **the server-side levers proposed for the other two
operations do not apply here.** What remains is the driver and the fixed command path.

### M1 — Pool checkout and server selection · PyMongo · **partly done, and not 5.6 µs**

**The 27.3 µs this line used to quote was measured on PyMongo 4.12 and no longer holds**:
re-measured on driver master the checkout layer is 13.5 µs, because master replaced the
`@contextlib.contextmanager` generators with context-manager classes and put the CMAP telemetry
behind an enablement check.

Taking the pool mutex once instead of eight times (`Pool.lock`, `Pool.size_cond` and
`Pool._max_connecting_cond` are three `Condition` views over one mutex) is 3.0% of retired
instructions — draft PR `carsontung666/mongo-python-driver#3`.

**The server-selection half is withdrawn**: it silently bypassed `MongoClient(server_selector=...)`,
a public option, and its 4.0% held only on a workload of pure `find_one` — see
`get_entity_driver.md` §2 C1a.

**An earlier version of this line quoted "5.6 µs per command combined". That is withdrawn**: it was
3.2 + 2.4, each obtained by applying an *instruction* percentage to a client-CPU number, which is
the conversion `get_entity_driver.md` §0 says is never performed. No combined figure for the pool
and selection changes is claimed.

### M2 — Skip `Cursor` construction for single-batch replies · PyMongo · **done, 12.1 µs per command**

An `_id` lookup is naturally `limit:1`/`singleBatch`, so the driver knows before it sends that the
reply cannot need a cursor. **Built and measured**: `Collection._find_one_single_batch` on branch
`find-one-fast-path`, draft PR `carsontung666/mongo-python-driver#1`. Paired 14 blocks × 500 on
this operation's own shape: **−12.1 µs of client CPU, 15.6%, 14/14 blocks**; −10.4 µs of wall,
6.2%. Wire-identical including key order, monitoring events identical, 1004 driver tests pass.
Details and the ten review defects fixed: `get_entity_driver.md`.

The driver term measured on this operation's own shape is **60.3 µs** against `get_node`'s 63.0;
paired difference −1.5 µs, block range [−9.2, +11.2], inside the spread. So the transfer holds, but
the term is not strictly query-independent — this reply is 1,160 bytes against `get_node`'s 613.

For scale: PostgreSQL's *entire* client-side term for this operation is 52.3 µs (71.787 wall − 19.5
server CPU). That figure includes the relay and kernel time whereas MongoDB's 60–63 µs is CPU only,
so the two are not like-for-like — but the comparison is conservative in the direction claimed.

### M3 — The fixed command path · `mongod` · ~24 µs, and it is flat

§2b settles what this is made of. Of the 23.9 µs above MongoDB's own per-command floor, and of the
45.4 µs total, **27.2 µs is MongoDB's own code** — but spread across allocator traffic (2.0 µs),
refcounting (0.9), BSON re-walking (0.9), WiredTiger config and search (1.1), command dispatch
(9.3 across eight named steps, largest 4.2) and collection acquisition (4.6). The largest single
mongod leaf frame in the whole operation is `operator new` at 1.1 µs.

**So there is no single server-side change worth attacking here.** That is a finding, not an
absence of one: it says the residual is per-command machinery, which is exactly what §6's coalescing
result (23.3× at B=64) predicts, and it is the same shape the driver side turned out to have.

§5 shows that after `IDHACK` there is still 23.9 µs of server CPU above MongoDB's own per-command
floor, in dispatch, IDL parse, collection acquisition, projection AST and executor construction. The
inclusive profile in §2 shows where it sits. This is the term that survives every planner change, and
it is the reason the other operations cannot be fixed by planner work alone.

§2a partitions it. Two proposals follow, both small in absolute terms and both on work this
operation provably cannot use.

### M4 — Do not build or probe a plan-cache key on the `IDHACK` path · `mongod` · 1.185 µs

`plan` is 2.164 µs on an operation that skips planning entirely, and 1.185 µs of it is
**`plan-cache KEY build` 0.599 + `plan-cache LOOKUP` 0.586**. The fast path is chosen before a
cached plan could be used and never contributes one, so both are spent on a cache this operation
neither reads usefully nor writes. 2.6% of the operation, and it also lands on every other
`IDHACK` query in the server.

### M5 — Skip the projection dependency analysis a `PROJECTION_SIMPLE` cannot need · `mongod` · 1.412 µs

`proj parseAndAnalyze` 0.906 + `proj optimize` 0.506, for the two-field inclusion projection
`{_id: 1, text: 1}`. The plan chosen is `PROJECTION_SIMPLE`, the case where no dependency analysis
is required. 3.1% of the operation.

Alongside them, smaller and listed for completeness rather than proposed: `MatchExpressionParser::parse`
0.761 µs to parse `{_id: <string>}`, which `IDHACK` does not evaluate; `CurOp::startTime`
0.612 µs of `clock_gettime`; `endQueryOp` index-usage statistics 0.594 µs.

**None of these is large.** Together M4 and M5 are 2.6 µs, 5.7% of server CPU and about 1.8% of the
0.142 ms operation. They are recorded because they are real and specific, not because they close the
gap — §2a's own finding is that they cannot, since 60% of the operation is transport and dispatch.

### Not applicable here

- **Plan caching** (`get_node.md` §5 M2): `get_entity` stays on `IDHACK` under both engines and moves
  **−0.05%** under `trySbeEngine` (`v8_sbe_otherops.json`). Excluded from that result's range for
  this reason.
- **A fast path for unique compound-index equality** (`get_node.md` §5 M1): already has one.
- **Dropping the hint**: there is no hint.

---

## 5. The ceiling this operation places on the other two

One harness (`bottleneck_20260806`), one floor, units held constant:

| quantity | µs | source |
|---|---|---|
| MongoDB per-command floor — `ping`, logging off, server CPU | 21.48 | `mongo_cpu_arms_nolog.json` |
| MongoDB `get_node` server CPU | 71.70 | same |
| MongoDB `get_entity` server CPU | 45.35 | same |
| PostgreSQL floor — `SELECT 1`, prepared / unprepared | 16.0 / 20.5 | `pg_psycopg_cpu.json` |
| PostgreSQL `get_node`, prepared / unprepared | 20.5 / 46.5 | same |
| PostgreSQL `get_entity`, prepared / unprepared | 19.5 / 30.0 | same |

Derived, all server CPU:

- **`get_node`'s query-addressable ceiling: 71.70 − 21.48 = 50.2 µs.** No query-side change can
  remove more, because `mongod` cannot serve any command below the floor.
- **A fast path already realises 71.70 − 45.35 = 26.3 µs of it**, just over half.
- **What remains after it: 45.35 − 21.48 = 23.9 µs**, the fixed command path — M3 above.
- **PostgreSQL `get_entity` above its own floor: 3.5 µs prepared, 9.5 µs unprepared.**

**The cross-engine contrast must use matched arms.** MongoDB re-plans on every call, so the honest
match is PostgreSQL *unprepared*: on `get_node` that is 50.2 µs above floor against **26.0 µs**, a
factor of 1.9. Against PostgreSQL's *prepared* arm the same contrast reads 50.2 against 4.5, a factor
of 11 — an earlier version quoted the second without naming the arm, inflating it about 5.8×.

**What this supports:** planner work is not where the remaining headroom is. Even with planning
entirely removed, MongoDB retains ~24 µs of server CPU above its own floor on a point lookup.

**What it does not support:** any claim that `get_entity` *is* the floor. At 45.35 µs it is 2.1× the
21.48 µs floor, with 53% of its server CPU above it, and §2 attributes that residual to real work.

**A separate floor, correctly labelled.** A hand-written 40-line `OP_MSG` client doing a bare `ping`
over the *container IP*, bypassing the relay, costs 35.9 µs wall = 21.3 server CPU + 14.6 client CPU
+ ~0 residual. That is a floor measurement, not a proposal, and not the transport any figure here
uses. Over the published port every benchmark arm crosses, PyMongo's `ping` is **90.1 µs wall = 21.2
server CPU + 45.8 client CPU + 23.1 µs residual**, the residual being the relay.

---

## 6. Application-side workarounds, and what is not measured

**Coalescing** — 23.3× at B=64, ~2.57× at B=3, logging-corrected from `report.tex:1105` (the
uncorrected table figures at `:1093` and `:1516` are 25.1× and 2.62×). This is by far the largest
lever on this operation, and it works precisely because the cost is per command rather than per row —
independent confirmation that M1, M2 and M3 are aimed at the right term. It is not a MongoDB change.

Not measured, stated plainly:

- **The exclusive phase partition has been produced** — §2a. The claim that it had not, which stood
  in earlier versions of this file, was wrong: `get_entity_phases.txt` was written by the same pass
  and on the same day as `get_node_phases.txt`.
- **No client/server split against PostgreSQL exists for this operation** — the gap-locus table
  covers the other three only.
- **`get_entity` appears at 0.228 ms in some artifacts and 0.142 ms in others, and logging is not the
  main reason.** `bench_entity_rootcause.py:562` and `:662` pin `"hint": "_id_"`, and **any hint
  disqualifies `IDHACK`** — that arm plans a full `FETCH`/`IXSCAN` and is a different operation. The
  `slowms=0` log line accounts for at most 27 µs of the 86 µs disagreement, roughly 31%.
