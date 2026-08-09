# master vs 7.0.34 on the get_entity shape — how this was set up

The 45.354 µs the report quotes for `get_entity` is from the containerised 7.0.34 that holds the
shared dataset. Comparing a locally built master against that number directly would compare two
transports as well as two versions: the container arm crosses a published docker port and this
host's endpoint-protection modules, which the origin split puts at 3.99 µs of the operation. So
both arms are host processes on loopback, over the same collection, alternated within blocks.

## The two servers

```
# 7.0.34, taken out of the image the shared instance runs
CID=$(docker create mongo:7); docker cp "$CID:/usr/bin/mongod" /tmp/entity-vercmp/bin/mongod-7034
docker rm -f "$CID"

# master, clean, at the pinned base -- the fork's own working tree carries another
# agent's uncommitted express work, so it cannot be used as a baseline
cd /home/junyao/code/mongo
git worktree add --detach /tmp/mongo-entity-baseline 0561c098b99ac5e929005e70a2e37d7a97a82423
cd /tmp/mongo-entity-baseline && bazel build --config=opt //src/mongo/db:mongod
```

Both are started with the same options, on loopback, with an 8 GB WiredTiger cache and their own
dbpath under `/tmp/entity-vercmp/`.

## The collection

`bench/db/load_entity_dataset.py` writes 9,000,000 documents matching the real collection's shape:
7-digit string `_id` from `1000000`, 120 words per document from the same vocabulary, ~1054 B of
BSON. Every document is a pure function of its `_id` — a PRNG seeded on the id — so the two servers
get identical bytes no matter how many loader workers run or in what order they finish.

It is not a copy of the real collection. Measured against it: 2,745 MB of storage against the real
3,269 MB and 125 MB of index against 133 MB. The text compresses about 16% better, which is the one
thing to hold against any cache-sensitive conclusion drawn here.

The loader refuses to write to port 57017.

## The instrument

`bench/db/bench_entity_version_ab.py`, reusing `bench_bottleneck_cpu.thread_snapshot`: CPU
nanoseconds burned by mongod's `conn*` threads over an arm, from
`/proc/<pid>/task/<tid>/schedstat` field 1, divided by the operations in that arm, with the idle
draw over the same wall time subtracted. Client cost is outside the counter and is not mixed in.

Both arms are checked before measuring: same document count, same winning plan is printed, and
every block asserts both arms returned a document for every id.

## The box has to be quiet

A null test — both arms pointed at the *same* 7.0.34 server, so the true delta is zero — was run
while the master build was still going, at load average 135:

```
arm           server CPU us     wall us
null_A               196.28       398.0
null_B               216.49       415.9
paired null_A - null_B: -20.21 us  [-174.44, +189.51]  2/4 blocks
```

196 µs against the ~45 µs the same operation costs on a quiet box, and a spread of ±190 µs on a
delta that is zero by construction. This is the same effect that made client CPU read 79 µs at load
3 and 318 µs at load 216: contention changes the cycles a fixed amount of work costs.

**So the null test is the gate.** The comparison is only run once it reports a delta inside a
microsecond or two, and it is re-run alongside the real measurement rather than assumed.
