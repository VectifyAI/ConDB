# Exhaust cursors work through `mongos`; PyMongo refuses them anyway

## The question

PyMongo refuses to open an exhaust cursor when it is connected to a router
(`pymongo/synchronous/cursor.py`, checked against 4.12.0):

```python
if self._cursor_type == CursorType.EXHAUST:
    if self._collection.database.client.is_mongos:
        raise InvalidOperation("Exhaust cursors are not supported by mongos")
```

`mongos` appears to support them:

- `src/mongo/s/commands/strategy.cpp:488` —
  `opCtx->setExhaust(OpMsg::isFlagSet(m, OpMsg::kExhaustSupported))`
- `src/mongo/s/commands/query_cmd/cluster_getmore_cmd.h:111-115` —
  `if (opCtx->isExhaust() && response.getCursorId() != 0) reply->setNextInvocation(boost::none);`
- `src/mongo/s/commands/strategy.cpp:1332-1337` — propagates `shouldRunAgainForExhaust` into the
  `DbResponse`

**Is the restriction still needed, and if so, what is it protecting against?** The error message
says the server does not support the feature, and that does not match the code or the behaviour
below.

This cannot be settled through PyMongo: the driver refuses before anything reaches the wire, so any
experiment run through it is unable to distinguish "mongos cannot do this" from "the driver will not
ask". `exhaust_probe.py` therefore speaks OP_MSG over a raw socket and sets `exhaustAllowed` itself.

## What we observe

The probe counts how many replies come back for a **single** `getMore` request, reading the
`moreToCome` bit out of `flagBits` rather than inferring anything from timing.

| endpoint | replies to one `getMore` | arrived unsolicited | rows |
|---|---|---|---|
| standalone `mongod` (control, to validate the probe) | 11 | 10 | 11,686 |
| `mongos`, one shard | 20 | 19 | 20,000 |
| `mongos`, two shards, results merged by `mongos` | 20 | 19 | 20,000 |

`mongos` streams, and it keeps streaming when it is merging two shards.

Same client, same socket, same `batchSize`, toggling only the `exhaustAllowed` flag; paired with
arms alternating within blocks:

| cluster | exhaust vs sequential `getMore` | blocks faster | median |
|---|---|---|---|
| one shard | **−23.41%** [−26.65, −8.91] | 8/8 | 20.2 ms vs 26.0 ms |
| two shards, run A | −19.40% [−56.15, +7.86] | 6/8 | 39.3 ms vs 52.0 ms |
| two shards, run B (this script, idle machine) | **−24.31%** [−30.51, −17.05] | 6/6 | 19.2 ms vs 25.8 ms |

Both two-shard runs are reported. Run A was taken on a machine with other work on it and is noisy
enough that its range crosses zero; run B was taken on an idle machine with the script in this
directory and is tight. They agree in direction and roughly in size with the single-shard result.
Expect the timings to move with hardware — the protocol result above is the part that should
reproduce exactly.

## What this does not show

It does **not** show that the restriction is safe to remove. An exhaust cursor holds its connection
for the cursor's lifetime, which has connection-pool consequences behind a router fronting many
shards that a single-cursor probe cannot see. Untested here: retryable reads, load-balancer mode,
failover and stepdown, auth, `maxTimeMS`, cursor abandonment, and any interaction with sessions or
transactions.

Environment: one `mongos`, two single-node shard replica sets, one config server, no auth, no load
balancer, a synthetic 20,000-document collection, and a Python probe that is not a production
driver. It is sized to answer a protocol question.

The narrow claim: **`mongos` streams exhaust replies correctly, including across a two-shard merge,
so "not supported by mongos" is not an accurate reason for the client-side refusal.** Whatever the
real reason is, it would be useful to have it written down.

## Why this matters for our workload

We are measuring MongoDB against PostgreSQL for an agent-memory workload whose dominant read is
"fetch an entire subtree" — a covered range scan returning 5 MB in the median case. About 40–45% of
that operation's wall time is the client blocked waiting for the first byte of a reply, because the
server fills a whole batch before transmitting while PostgreSQL streams row by row. An exhaust
cursor recovers most of that. We had recorded it as unavailable to sharded deployments, on the
strength of the driver's error message, and that conclusion turns out to rest on a claim about the
server that is not true.

## Reproducing

Requires a MongoDB build (`mongod`, `mongos`) and `pymongo` for its `bson` module.

```bash
export MONGO_BIN=/path/to/build/bin      # directory containing mongod and mongos
./setup_cluster.sh                       # config + 2 shards + mongos on 127.0.0.1:57022
python3 exhaust_probe.py --port 57022 --filter-path /000006/000075/000773
python3 exhaust_probe.py --port 57022 --filter-path /000006/000075/000773 --measure
./setup_cluster.sh --teardown
```

The first probe prints the protocol result; `--measure` prints the paired timing comparison.
Point `--port` at a standalone `mongod` to see the control.
