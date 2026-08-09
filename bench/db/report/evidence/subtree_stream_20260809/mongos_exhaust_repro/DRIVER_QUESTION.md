# Draft for the drivers team: why does PyMongo refuse exhaust cursors behind `mongos`?

Text intended to be filed as a question (PYTHON project) or raised directly. Kept short; the
evidence and its limits are in `README.md`, and the whole thing reproduces with the two scripts
beside this file.

---

**Summary**

`Cursor` raises `InvalidOperation("Exhaust cursors are not supported by mongos")` whenever the
client is connected to a router. On MongoDB master, `mongos` does support exhaust cursors: it reads
`exhaustAllowed` off the request, continues the exhaust stream on `getMore`, and sets `moreToCome`
on the replies. We verified this on the wire, including with `mongos` merging two shards. The
restriction may still be the right behaviour, but the stated reason does not match the server, and
we could not find the real one written down.

**Where the refusal is**

`pymongo/synchronous/cursor.py` (4.12.0), in two places — `_check_okay_to_chain` /
`add_option`:

```python
if self._cursor_type == CursorType.EXHAUST:
    if self._collection.database.client.is_mongos:
        raise InvalidOperation("Exhaust cursors are not supported by mongos")
```

**What the server does**

- `src/mongo/s/commands/strategy.cpp:488` —
  `opCtx->setExhaust(OpMsg::isFlagSet(m, OpMsg::kExhaustSupported))`
- `src/mongo/s/commands/query_cmd/cluster_getmore_cmd.h:111-115` —
  `if (opCtx->isExhaust() && response.getCursorId() != 0) reply->setNextInvocation(boost::none);`
- `src/mongo/s/commands/strategy.cpp:1332-1337` — propagates `shouldRunAgainForExhaust` into the
  `DbResponse`

**What we measured**

Because the driver refuses before anything reaches the wire, we used a raw OP_MSG client that sets
`exhaustAllowed` itself and counts replies to a single `getMore` by decoding `flagBits`.

| endpoint | replies to one `getMore` | unsolicited | rows |
|---|---|---|---|
| standalone `mongod` (control) | 11 | 10 | 11,686 |
| `mongos`, one shard | 20 | 19 | 20,000 |
| `mongos`, two shards, merged | 20 | 19 | 20,000 |

Same client and socket, toggling only `exhaustAllowed`, paired with arms alternating in blocks:
**−23.4%** (8/8 blocks) single-shard, **−24.3%** (6/6) across two shards on an idle machine.

**Why we care**

Our workload's dominant read returns a whole subtree — a covered range scan, ~5 MB in the median
case. Roughly 40–45% of that operation's wall time is the client blocked waiting for the first byte
of a reply, because the server fills a batch before transmitting. An exhaust cursor recovers most of
it. We had written off sharded deployments on the strength of this error message.

**What we are not claiming**

Not that the restriction should simply be dropped. An exhaust cursor holds its connection for the
cursor's lifetime, and behind a router fronting many shards that has pooling consequences a
single-cursor probe cannot evaluate. We did not test retryable reads, load-balancer mode, failover,
auth, `maxTimeMS`, or cursor abandonment. Our environment was one `mongos`, two single-node shards,
no auth, and a probe that is not a production driver.

**The question**

Is the restriction still load-bearing, and if so, what for? If it is about connection pinning rather
than server capability, would it be worth saying so in the error message — and is there a shape
(explicit opt-in, a dedicated connection, a cap on concurrent exhaust cursors) that would let
sharded deployments use it?
