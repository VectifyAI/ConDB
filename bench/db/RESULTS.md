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
and MongoDB's P95 blows out to 2.5 s versus ~47-135 ms for the others. For the
PageIndex workload this is the single read concern, and it is a tail-latency
problem, not a median one.

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

## MongoDB on the PageIndex workload

Across the operations ConDB actually issues, MongoDB is competitive everywhere
except one place:

- point lookup, expand-children, content fetch: sub-millisecond, fine.
- write path: no MVCC dead-tuple accumulation (unlike PostgreSQL) for trace/index edits.
- concurrency: scales the furthest, lowest P99 of the server engines.
- storage: smallest on disk (via compression).
- **get_subtree at scale: the one weakness.** Pulling a large subtree is ~3x the
  median of the row engines (31 ms vs 11 ms) and ~20x worse at the tail
  (2.5 s P95 vs ~47-135 ms).

The reason is structural. MongoDB's unit of read is the whole document: an index
finds the matching nodes quickly, but to return any non-indexed field it must
FETCH the full BSON document and apply the projection. For one node that is
fast; for the 36k nodes in a large subtree it is 36k fetches. The data is
cache-resident (and pages in the WiredTiger cache are stored uncompressed), so
the cost is neither disk I/O nor decompression: it is the per-document work of
locating each record and materializing the full document, `text` payload
included, only to project out a node id, plus cursor batching of the results;
that is the multi-second tail. The other operations touch few documents, so
they stay fast.

## Optimization plan (no engine changes; MongoDB Community Edition)

The target is `get_subtree`; the others need nothing. All of this is index and
schema design on the community version, no source changes.

1. Covering index on `path` plus the fields the tree view renders (`node_id`,
   `title`, `summary`), projecting those and `_id: 0`. `get_subtree` then runs
   index-only and never fetches each node's large `text` body, which is the
   source of the tail. Highest leverage, pure configuration; the cost is a
   larger index.
2. Split the large `text` field into a separate collection (structure plus
   summary in the node, content keyed by node id). Smaller hot documents make
   every FETCH and the working set cheaper.
3. Nested-document subtree model. Store bounded subtrees as single nested
   documents so `get_subtree` is a handful of reads instead of 36k. The 16 MB
   document limit makes this compose with the text split (item 2): a full-text
   subtree would exceed the limit several times over, while structure-and-summary
   buckets of a few levels each stay well under it. Largest potential win; the
   trade-off is that per-node updates become rewrites of the enclosing document.
4. Keep the materialized-path range form of `get_subtree`, not recursive
   `parent_id` traversal.

Point/children/content reads, writes, concurrency, and storage are already
MongoDB's strengths and need no work.

## Caveats

- Localhost, single node; concurrency capped at 64 client processes.
- One tree per database; multi-tenant selectivity not exercised.
- Synthetic content compresses optimistically.
- Only the PageIndex document tree is benchmarked. The other generated shapes
  (chatindex, filesystem, generic, embeddings, corpus) are produced but not yet
  wired into the read benchmark.
