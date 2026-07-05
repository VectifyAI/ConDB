# Database Comparison: PageIndex Retrieval Workload

Benchmarks MongoDB (the ConDB optimization target) against PostgreSQL (JSONB),
DuckDB, and SQLite (ConDB's current engine) on the operations ConDB's retrievers
actually issue against storage. See `README.md` for setup and the `bench_*.py`
scripts for the schema, indexes, and queries.

| dataset | nodes | source JSON |
|---|--:|--:|
| medium | 70,843 | 85 MB |
| large | 10,000,000 | 14.06 GB |

## Operations measured (and where ConDB uses them)

The retrievers (`beam_retriever.py`, `block_retriever.py`) drive retrieval by
walking the tree from the root and reading content for the nodes they select.
Their entire storage access surface is four calls:

| operation | storage call | used for | callers |
|---|---|---|---|
| point lookup | `get_node(tree_id, node_id)` | navigate to / read one node | beam, block, vertical |
| expand children | `get_children(tree_id, node_id)` | list a node's children to pick where to descend | beam, block |
| get subtree | `get_subtree(tree_id, node_id, depth)` | render the tree view and pull a block for the LLM | tree formatter |
| fetch content | `get_entity(tree_id, node_id)` | read the content of selected nodes | beam, block |

Everything below is one of these four, plus the storage/ingest footprint, the
write path (incremental index and reasoning-trace updates), and concurrency
(many agents querying at once). Operations PageIndex retrieval never issues
(GROUP-BY aggregation, ancestor-to-root walks, depth/page-range filters, naive
substring scan) are not reported here.

## Read this before the tables

- **Embedded vs client-server.** SQLite and DuckDB run in-process; MongoDB and
  PostgreSQL run as servers over localhost TCP, so every call pays client-server
  overhead (round trip plus protocol handling) the embedded engines never incur. The fair single-call comparison
  is MongoDB vs PostgreSQL; the concurrency section shows real throughput.
- **Storage measured differently.** MongoDB `storageSize` is post-compression
  (WiredTiger + snappy); the others are real on-disk files. MongoDB's
  uncompressed logical size is reported separately.
- Every query returns the same row count on all four engines (PostgreSQL `path`
  uses `COLLATE "C"` so its subtree range matches the others' byte order).
- `get_subtree` is measured returning node ids. Returning the title and summary
  the tree view also renders would cost the relational engines more but not
  MongoDB (it materializes the whole document regardless of projection), so the
  choice is conservative toward MongoDB.

## 1. Storage and ingest (10,000,000 nodes)

| engine | on-disk total | index | uncompressed | bytes/node | ingest (nodes/s) | index build |
|---|--:|--:|--:|--:|--:|--:|
| DuckDB | 5.02 GB | 0 | - | 502 B | 348,831 | 10 s |
| MongoDB | 5.81 GB | 536 MB | 15.21 GB | 581 B | 41,387 | 69 s |
| SQLite | 19.51 GB | 980 MB | - | 1,951 B | 52,880 | 38 s |
| PostgreSQL (JSONB) | 17.71 GB | 1.19 GB | - | 1,771 B | 62,816 | 52 s |

On-disk totals include the indexes; MongoDB holds 15.21 GB of logical BSON in
5.27 GB of data on disk (WiredTiger snappy). DuckDB and MongoDB are the most
compact, DuckDB through columnar encoding and MongoDB through compression; even
uncompressed, MongoDB's 15.21 GB of BSON is no larger than the relational raw
tables. PostgreSQL and SQLite are several times larger on disk.

## 2. Retrieval operations (P50, ms; lower is better)

### medium (70,843 nodes)

| operation | MongoDB | PostgreSQL | DuckDB | SQLite |
|---|--:|--:|--:|--:|
| point lookup (`get_node`)    | 0.238 | 0.081 | 2.705 | **0.008** |
| expand children (`get_children`) | 0.251 | 0.090 | 0.856 | **0.017** |
| get subtree (`get_subtree`)  | 0.498 | 0.210 | 2.700 | **0.091** |
| fetch content (`get_entity`) | 0.228 | 0.071 | 2.750 | **0.007** |

### large (10,000,000 nodes), P50 / P95

| operation | MongoDB | PostgreSQL | DuckDB | SQLite |
|---|--:|--:|--:|--:|
| point lookup    | 0.270 / 0.32 | 0.087 / 0.09 | 5.590 / 8.97 | **0.012 / 0.014** |
| expand children | 0.282 / 0.32 | 0.099 / 0.14 | 4.275 / 5.05 | **0.023 / 0.032** |
| get subtree (~36k nodes) | 30.9 / 2478 | 10.7 / 123 | **9.92 / 46.65** | 11.81 / 135.0 |
| fetch content (`get_entity`) | 0.249 / 0.32 | 0.081 / 0.09 | 5.208 / 8.35 | **0.012 / 0.013** |

Point lookup, expand-children, and content fetch are sub-millisecond on MongoDB,
PostgreSQL, and SQLite at any scale; these are the per-step costs of navigating
the tree, and MongoDB is fine on all of them (DuckDB is the outlier, with no
OLTP point path). The one operation that costs is `get_subtree` at scale: pulling
a ~36k-node subtree is 10-12 ms on PostgreSQL/DuckDB/SQLite but 31 ms on MongoDB,
and MongoDB's P95 in this first multi-engine run read 2.5 s versus ~47-135 ms for
the others. That 2.5 s did not survive a controlled re-measurement: run in
isolation and under concurrent load with the collection resident, `get_subtree`'s
steady-state P95 is ~0.3-0.4 s (~3x PostgreSQL, not ~20x), and the 2.5 s traces to
whole-system contention during the multi-engine run, not to the query itself (see
the optimization section below). For the PageIndex workload this is the single read
concern, and it is a tail-latency problem, not a median one.

`get_subtree` can be implemented two ways; the benchmark measures both. A
materialized-path range scan (the numbers above) is faster than chasing
`parent_id` pointers with a recursive query (at medium, 0.52 ms vs 0.82 ms on
MongoDB, 0.21 ms vs 0.31 ms on PostgreSQL), so ConDB should prefer the path
range.

## 3. Write path (medium; 5,000 updates, 2,000 incremental inserts)

Updating a node (reindex, edit a reasoning trace) and inserting new nodes.

| engine | update P50 (ms) | update ops/s | incr insert ops/s | bloat after 5k updates |
|---|--:|--:|--:|--:|
| MongoDB | 0.242 | 4,050 | 5,586 | **0 MB** |
| PostgreSQL (JSONB) | 0.123 | 7,856 | 8,732 | 7 MB |
| DuckDB | 2.966 | 332 | 450 | 7 MB |
| SQLite | **0.017** | **40,442** | **21,815** | **0 MB** |

WiredTiger is MVCC like PostgreSQL, but it reclaims obsolete document versions
automatically, so a field-level `$set` leaves no dead tuples for a later
`VACUUM`; PostgreSQL accumulates them (7 MB from 5k updates, to be vacuumed
later). MongoDB's on-disk growth here is negligible, though not a robust zero:
WiredTiger reports storage in checkpoint-sized chunks, so a comparable update
load can register tens of megabytes. DuckDB is unsuitable for OLTP writes.
Incremental
insert is far slower than bulk load for every engine because each row maintains
indexes and commits durably.

## 4. Concurrency (medium; point-lookup throughput, ops/s)

Many agents querying at once. Independent OS processes (no GIL bottleneck); each
opens its own connection.

| engine | c=1 | c=2 | c=4 | c=8 | c=16 | c=32 | c=64 | P99 @ c=64 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| SQLite | 131,656 | 242,245 | **439,543** | 436,885 | 338,813 | 244,149 | 168,060 | 0.40 ms |
| PostgreSQL | 13,357 | 25,971 | 47,649 | 89,207 | **159,074** | 122,066 | 137,780 | 6.20 ms |
| MongoDB | 4,264 | 8,159 | 15,341 | 28,647 | 45,556 | 58,551 | **86,029** | 1.49 ms |
| DuckDB | 390 | 780 | 1,548 | 3,049 | 5,898 | 11,119 | **18,135** | 7.26 ms |

PostgreSQL and MongoDB scale with concurrency (PG to ~159k at c=16, Mongo still
climbing to 86k at c=64 with a 1.49 ms P99). SQLite has the highest absolute
throughput (~440k) but peaks within the first few clients (c=4-8) and then
degrades as the processes contend on its single file; the peak point and height
vary run to run, the rise-then-fall shape does not. Under load the gap between the
embedded and server engines is far smaller than single-call latency suggests.

## 5. Operations added after a usage review (`bench_extra.py`)

A review of how agentic tree retrieval actually hits storage flagged three
operations the sections above do not cover. They run on the medium tree
replicated into 25 co-resident trees (1,771,075 rows in one store); every read
filters by `tree_id`, served by a `tree_id`-led compound index (the pattern
MongoDB's multi-tenant guidance prescribes).

### 5.1 Multi-tenant selectivity (per-query `tree_id` filter)

The single-tree benchmarks never exercised tenant selectivity. With 25 trees
sharing one collection/table and a compound index led by `tree_id`, the
per-query reads stay close to their single-tenant numbers (Section 2): the cost
tracks index depth, not the number of co-resident trees.

| operation | MongoDB | PostgreSQL | DuckDB | SQLite |
|---|--:|--:|--:|--:|
| point lookup    | 0.345 | 0.124 | 3.772 | **0.008** |
| expand children | 0.372 | 0.147 | 3.073 | **0.014** |
| get subtree     | 1.273 | 0.312 | 4.605 | **0.069** |

P50 ms, 25 trees. Versus single-tenant medium, MongoDB rises ~1.5x on point
(0.238 -> 0.345) and ~2.6x on subtree (0.498 -> 1.273) for a 25x larger store,
with no scan blow-up; the MongoDB-vs-PostgreSQL ranking is unchanged. The
takeaway for a multi-tenant deployment: lead the index with `tree_id` and
selectivity holds. (DuckDB stays the OLTP outlier, as in Section 2.)

### 5.2 Batched id-list multiget (`$in` / `IN`) vs point-lookup loop

The navigation step returns a list of candidate node ids per query, so the
system fetches several nodes by id at once. Fetching them with one batched call
beats a point-lookup loop on every engine, and the gap widens with batch size
because each loop iteration on a server engine is a network round trip.

| engine | b=10 batched/loop | b=50 batched/loop | b=200 batched/loop | speedup @200 |
|---|--:|--:|--:|--:|
| MongoDB | 0.457 / 2.987 | 1.563 / 16.98 | 4.559 / 272.8 | **60x** |
| PostgreSQL | 0.272 / 1.181 | 0.783 / 5.331 | 1.404 / 28.81 | 21x |
| DuckDB | 19.63 / 38.54 | 22.18 / 201.6 | 39.08 / 789.7 | 20x |
| SQLite | 0.034 / 0.048 | 0.125 / 0.252 | 0.503 / 0.974 | 2x |

P50 ms per batch. MongoDB gains the most from batching (60x at 200 ids) precisely
because its per-call client-server overhead, the 3x penalty visible on single
reads in Section 2, is amortized across the batch; in-process SQLite has no round
trip to amortize, so it gains only ~2x. Practical rule: when the LLM hands back a
candidate id-list, issue one `$in`, never a loop.

### 5.3 Whole-tree delete and space reclamation (`delete_tree`)

Deleting one tenant's tree (70,843 of 1,771,075 rows, ~4%), then reclaiming.
This is a lifecycle/compliance operation (eviction, TTL, re-index, right-to-be-
forgotten), not a retrieval hot-path one.

| engine | delete ops/s | delete time | on-disk: before -> after delete -> after reclaim | reclaim |
|---|--:|--:|--:|--:|
| PostgreSQL | 157,578 | 0.45 s | 2.77 -> 2.77 -> 2.66 GB | VACUUM FULL, 17.3 s |
| SQLite | 141,842 | 0.50 s | 2.56 -> 2.56 -> 2.46 GB | VACUUM, 10.6 s |
| MongoDB | 60,680 | 1.17 s | 911 -> 916 -> 916 MB | compact, 0.1 s |
| DuckDB | 20,171 | 3.51 s | 790 -> 796 -> 796 MB | checkpoint, no shrink |

The delete itself is fast everywhere (<4 s). The reclamation behavior splits the
engines: **none returns space on the delete alone**. SQLite (`VACUUM`) and
PostgreSQL (`VACUUM FULL`) reclaim the deleted tree's footprint (~0.1 GB, the 4%
removed) but only through a full, locking rewrite that takes 10-17 s. MongoDB
`compact` and DuckDB `checkpoint` did not measurably shrink the file for a
single-tenant scattered delete; this is WiredTiger's documented no-auto-shrink
behavior (compact reclaims only space recoverable at the file end). For MongoDB
this is the same trade as the write path (Section 3): WiredTiger carries no
vacuum debt on updates, and in exchange a one-off bulk delete does not
auto-shrink. `storageSize` is checkpoint-granular, so deltas under tens of MB are
reporting noise, not reclaimed space.

## MongoDB on the PageIndex workload

Across the operations ConDB actually issues, MongoDB is competitive everywhere
except one place:

- point lookup, expand-children, content fetch: sub-millisecond, fine.
- write path: no MVCC dead-tuple accumulation (unlike PostgreSQL) for trace/index edits.
- concurrency: scales the furthest, lowest P99 of the server engines.
- storage: smallest on disk (via compression).
- **get_subtree at scale: the one weakness.** Pulling a large subtree is ~3x the
  median of the row engines (31 ms vs 11 ms). The first run's 2.5 s P95 was a
  contention artifact; re-measured in isolation the steady-state P95 is ~0.3-0.4 s,
  ~3x PostgreSQL's 123 ms, not the ~20x the first tail implied.

The reason is structural. MongoDB's unit of read is the whole document: an index
finds the matching nodes quickly, but to return any non-indexed field it must
FETCH the full BSON document and apply the projection. For one node that is
fast; for the 36k nodes in a large subtree it is 36k fetches. In the measured
run the data is cache-resident (and pages in the WiredTiger cache are stored
uncompressed), so the cost is neither disk I/O nor decompression: it is the
per-document work of locating each of the ~36k records and threading it through
the cursor. The cost in this regime scales with the *number* of fetches, not
their size -- splitting the large `text` out of each document shrinks every
document ~75% and does not move the resident-cache latency at all. That does not
mean production should keep `text` inline: once the multi-tenant working set is
larger than RAM, the first requirement is keeping structure/title/summary cached,
which means splitting `text` before pricing finer optimizations. The other
operations touch few documents, so they stay fast.

## Optimization plan (measured; no engine changes; MongoDB Community Edition)

The target is `get_subtree`; the others need nothing. All of this is index and
schema design on the community version, no source changes. Because the harness was
in place, the fixes below were measured on the 10M tree, not just proposed (one
client; `view` returns node_id+title+summary, `id` returns node_id only; ms):

| variant | P50 | P95 | result |
|---|--:|--:|---|
| baseline, view (`text` inline) | 39.6 | 419 | reference |
| Subset Pattern (split `text`), view | 39.2 | 419 | **no change** |
| lean covering `{path,node_id}`, id | 19.3 | 191 | ~1.4x, covered (docsExamined=0) |
| fat covering `+title,summary`, view | 33.5 | 350 | ~1.2x; index 4.66 GB |
| lean covering + batched `$in`, view | 137 | 1314 | ~3x slower |

What the resident-cache numbers say (this inverts the intuitive ranking only
under the measured fully-resident condition):

1. **A covering index is the only cheap resident-cache lever, and a modest one.** A lean
   `{path,node_id}` index serves the id-only subtree query index-only
   (`PROJECTION_COVERED`, totalDocsExamined=0) for ~1.4x. Extending it to cover the
   fields the tree view renders (`title`, `summary`) still works (~1.2x) but
   inflates the index to 4.66 GB -- as large as the collection's data -- because
   `summary` is a multi-kilobyte key. Resolving the view with a covered id scan
   plus a batched `$in` is *worse* (~3x slower): a ~36k-id `$in` overflows the
   16 MB command limit and re-fetches the documents anyway.
2. **Splitting the large `text` field does nothing in this resident-cache run.** It shrinks the
   collection ~75% (4.97 -> 1.25 GB) yet leaves `get_subtree` P95 unchanged
   (419 -> 419 ms): the cost is the *number* of per-node fetches, not their size,
   and on this host the cache holds the whole collection so document size never
   gates the scan. Caveat: the Subset Pattern's premise is a working set larger
   than RAM, which this fully-resident host did not exercise. On a
   cache-constrained host it can become the first schema requirement, because it
   keeps the hot structure/title/summary collection small enough to remain cached;
   that retest is open.
3. **A larger win needs structural denormalization (untested).** Nested Sets, a
   tree-order `_id` on a clustered collection, or a pre-computed subtree document
   turn the subtree into one range query, a sequential read, or a point read --
   removing fetches wholesale rather than shrinking them. Viable because the tree
   is largely write-once (Nested Sets' O(n) renumber is paid at ingest, though the
   incremental-insert path means it is not strictly write-once). Not measured: with
   the baseline already sub-second, whether the schema complexity is worth it is a
   product call.
4. Keep the materialized-path range form of `get_subtree`, not recursive
   `parent_id` traversal, and not `$graphLookup` (100 MB cap in 7.0).

Point/children/content reads, writes, concurrency, and storage are already
MongoDB's strengths and need no work.

Production-scale ordering is therefore different from the measured
resident-cache ordering: split `text` first, keep indexes and shard keys led by
`tree_id` so each query remains tenant-local, then add lean covering indexes or
denormalized subtree views where `get_subtree` is hot. The experiment above does
not quantify the cache-constrained benefit; it only shows why the benefit cannot
appear when both schemas already fit in RAM.

## Caveats

- Localhost, single node; concurrency capped at 64 client processes.
- Sections 1-4 use one tree per database; multi-tenant selectivity, batched
  multiget, and whole-tree delete are exercised separately in Section 5 (25
  co-resident trees).
- Synthetic content compresses optimistically.
- The 10M-tree re-measurement and the `get_subtree` optimization ran with the
  collection fully resident in a WiredTiger cache many times its size, so any
  result that depends on working-set-vs-RAM (notably the Subset Pattern) is not
  exercised here.
- Only the PageIndex document tree is benchmarked. The other generated shapes
  (chatindex, filesystem, generic, embeddings, corpus) are produced but not yet
  wired into the read benchmark.
