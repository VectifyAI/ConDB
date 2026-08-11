# Why drivers refuse exhaust cursors through mongos — provenance

2026-08-11. Answers the question left open in `ops/get_subtree.md` §M1: what is the driver-side
refusal protecting against, given the server streams correctly through a two-shard merge? Three
evidence lines, gathered independently and cross-checked: the PyMongo git history (local full clone
at `/home/junyao/code/mongo-python-driver`), the server git history (pinned base `0561c098b99`),
and the public record (mongodb/specifications, jira.mongodb.org, other drivers' sources on GitHub).

## Answer

**The refusal is a version guard written in 2013 that was correct until MongoDB 7.1 (August 2023)
and has been stale since.** It is not mandated by any cross-driver specification — no revision of
any driver spec from 2015 to today has ever contained a mongos restriction on exhaust. MongoDB's
own backlog plans to remove it (PYTHON-4008, DRIVERS-3231), and libmongoc has already shipped the
removal, gated on wire version 22 (= 7.1). Nobody has done the PyMongo work.

## Server-side timeline (verified in git history and against Jira)

| Release | Commit | Ticket | What happened |
|---|---|---|---|
| 2.7.x (2014) | `98fb05b9cc` | SERVER-12750 | mongos actively rejects the OP_QUERY exhaust bit (error 18526) |
| 4.2 (2018) | `d04bafff3f` | SERVER-36105 | OP_MSG exhaust (`kExhaustSupported`) lands; mongod getMore exhaust starts here. mongos honored it too, as a side effect of the generic mechanism (`8b0a7b102b`, SERVER-41481) |
| 4.4 (2019) | `3fea6b3397` | SERVER-44517 | Exhaust refactored to opt-in `setNextInvocation()`; mongod's getMore opts in, mongos's cluster getMore does not — de facto removal, codified by the test `MongosIgnoresExhaustForGetMore`. Jira: removed "due to untested code paths" |
| 7.1 (2023) | `d5b53839c9` | SERVER-57297 | mongos getMore exhaust deliberately enabled — the whole change is 5 lines in `cluster_getmore_cmd.h` (:111-115 today). Old skip-tests deleted; `jstests/noPassthrough/query/exhaust_cursors.js` and `op_msg_integration_test.cpp` now assert exhaust works through mongos |

No server-side restriction remains at the pinned base: no load-balanced-mode gate (SERVER-57297's
description cites LB clusters as a *motivation*, since drivers already pin cursor connections
there), error replies terminate the stream cleanly, and an abandoned stream is reaped by a
synthesized `killCursors` in the shared `SessionWorkflow` (`session_workflow.cpp:1125-1156`).
Only getMore continues a stream; `find` never starts one. Error 18526 is gone from the tree.

## Driver-side provenance (PyMongo)

- The check — `if self._collection.database.client.is_mongos: raise InvalidOperation("Exhaust
  cursors are not supported by mongos")` — was introduced by `4d422586` (Bernie Hackett,
  2013-08-05, "Support exhaust cursor flag PYTHON-265", released in 2.6), when exhaust existed only
  as the OP_QUERY flag and mongos genuinely lacked it. No rationale comment; the era's server
  behavior is the rationale (see SERVER-12750 above, and 2.6-era `mongodump`'s own `!usingMongos`
  guard in `src/mongo/tools/dump.cpp`).
- `d26bf933` (2021, PYTHON-1636, released 4.0) moved user-facing exhaust to OP_MSG and left the
  check byte-for-byte intact — carried along, not re-affirmed. No commit since has removed,
  conditionalized, or discussed it.
- The restriction is enforced by PyMongo-authored tests only; nothing under `test/` comes from the
  cross-driver spec test corpus. One test cites SERVER-2627 (a 2011 ticket) as its reason.
- In load-balanced mode the check does not even fire (`is_mongos` tests for server type Mongos, LB
  reports LoadBalancer), so exhaust-through-a-router is already reachable in PyMongo behind an LB —
  with connection pinning, per the LB spec's generic cursor-pinning rules.

## The specs

- The find/getMore spec's exhaust section restricts only which opcode to use per server version;
  the word "mongos" has never appeared in it (checked 2015 original through current).
- OP_MSG spec: `exhaustAllowed` is legal on getMore (4.2+) and hello (4.4+); restrictions are
  per-operation, never per-topology. A client must consume the stream or close the connection.
- Load Balancer spec: "exhaust" appears nowhere; all cursors pin their connection in LB mode.
- Wire version featurelist: **7.1 / wire version 22 — "Exhaust Cursors Enabled for Sharded
  Clusters"**. This is the gate libmongoc uses (`WIRE_VERSION_MONGOS_EXHAUST 22`,
  `mongoc-cursor.c` ~:843-859).

## The tickets

| Ticket | Status | Content |
|---|---|---|
| [SERVER-57297](https://jira.mongodb.org/browse/SERVER-57297) | Fixed, 7.1.0 | mongos exhaust getMore support |
| [PYTHON-4008](https://jira.mongodb.org/browse/PYTHON-4008) | Backlog since 2023-10 | "We should add support for exhaust cursors on mongos 7.1+ (maxWireVersion 22)" |
| [DRIVERS-3231](https://jira.mongodb.org/browse/DRIVERS-3231) | Backlog, 2025-07 | Reintroduce driver exhaust as a performance optimization; plans a CRUD-spec `exhaust` option on find/aggregate. Notes the boundary: "mongos does not use exhaust when communicating with shards" |
| [DRIVERS-535](https://jira.mongodb.org/browse/DRIVERS-535) | Won't Do, 2025-07 | The old epic; closed for "lack of clear customer demand and incomplete support in sharded topologies" — the second reason ended with 7.1, the first is what this project's measurements address |
| [PYTHON-4007](https://jira.mongodb.org/browse/PYTHON-4007) | Fixed, 4.6 | Cautionary precedent: driver accidentally promoted all cursors to exhaust in LB mode |
| [CDRIVER-3487](https://jira.mongodb.org/browse/CDRIVER-3487) | Backlog | The one documented hazard: exhaust vs single-threaded SDAM monitoring — not applicable to PyMongo |

Only PyMongo and libmongoc (plus PHP via libmongoc) expose user-facing exhaust at all; Go, Node,
Java, Rust, C# use the exhaust wire mechanics only for streaming hello. libmongoc allows mongos at
wire ≥ 22; PyMongo still refuses categorically. They now disagree.

## Implications for this project

1. **The M1 question is answered; no "may we?" remains to ask MongoDB.** The driver change is
   pre-approved in their own backlog. A PyMongo PR can and should reference **PYTHON-4008** — a
   real ticket, so the no-invented-tickets rule is satisfied on this lane.
2. The correct shape of the change is libmongoc's: allow exhaust against mongos **iff
   maxWireVersion ≥ 22**, keep the refusal for older servers. Both `_supports_exhaust` and
   `add_option` sites, sync and async, plus the `find()` docstring and the three mongos-refusal
   tests.
3. The measured baseline (stock 7.0.34 on 57017) predates wire 22: exhaust through *mongos* cannot
   be demonstrated there. Standalone-mongod exhaust (what the ConDB benchmark exercises) has worked
   since 4.2 and is unaffected. The two-shard wire verification in
   `evidence/review_20260807_subtree/` ran against a 7.1+ build and stands.
4. Boundary to state in any PR: the win is the router→client leg only (mongos→shard traffic never
   uses exhaust, per DRIVERS-3231); connection is monopolized for the cursor's lifetime;
   abandonment must close the socket (PyMongo already does, `mongo_client.py` `_cleanup_cursor_lock`).
5. What is still worth raising with MongoDB, now sharpened to logistics: who reviews a PYTHON-4008
   PR; whether this project's measurements (−23.4% paired single-shard, −40% cohort-weighted bound
   on `get_subtree`) should be attached to DRIVERS-3231 as the demand evidence DRIVERS-535 lacked;
   and the timeline for the CRUD-spec `exhaust` option, since a PyMongo-only lift lands ahead of
   the spec.
