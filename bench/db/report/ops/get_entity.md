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

---

## 1. Where the gap is

| | MongoDB | PostgreSQL prepared | PostgreSQL unprepared |
|---|---|---|---|
| server CPU | 45.4 µs | 19.5 µs | 30.0 µs |

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

Its rows agree with the independent inclusive pass to three decimals where the two overlap —
projection `parseAndAnalyze` 1.998% against 2.00%, collection acquisition 8.027% against 7.99%,
release 2.044% against 2.04% — which is what says both ran over the same sample set.

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

**Environment note.** 8.39% of self samples land in EDR and container-networking kernel modules
(`dsa_filter`, `bmhook`, `tmhook`, `nf_tables`, `nf_conntrack`, `nf_nat`, `bridge`, `nft_compat`)
inside `mongod`'s connection thread. That inflates the transport terms and is a property of this box,
not of MongoDB. It is common-mode with the PostgreSQL psycopg arm.

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

### M1 — Pool-checkout fast path · PyMongo · 13.5 µs per command

Pool checkout and checkin on every operation against an already-pooled connection. **The 27.3 µs
this line used to quote was measured on PyMongo 4.12 and no longer holds**: re-measured on driver
master it is 13.5 µs, because master replaced the `@contextlib.contextmanager` generators with
context-manager classes and put the CMAP telemetry behind an enablement check. Not attempted.
Server selection, which the old figure bundled in, is a further 2.1 µs and has been done —
`get_entity_driver.md` §2 C1a. Full decomposition and the locking structure that makes the rest
tractable: `get_entity_driver.md` §2 C1b.

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

### M3 — The fixed command path · `mongod` · ~24 µs

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
