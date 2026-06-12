# PageIndex x Database Benchmark

Benchmarks MongoDB (the ConDB optimization target) against PostgreSQL (JSONB),
DuckDB, and SQLite on **PageIndex-shaped retrieval workloads** - large
hierarchical document trees with the access patterns ConDB actually issues
(point lookups, parent->child traversal, subtree expansion, metadata filters).

## Layout

```
bench/db/
  gen_pageindex.py     synthetic PageIndex tree generator (canonical format)
  gen_formats.py       generators for the other JSON shapes (see FORMATS.md)
  bench_databases.py   ingest + storage + retrieval (read) benchmark across engines
  bench_writes.py      write path: update latency, bloat, incremental insert
  bench_operators.py   operators: recursive traversal, join, aggregation
  bench_concurrency.py point-lookup throughput vs client count
  run_all.sh           run the full suite from scratch (read/write/operators/concurrency)
  report.py            render read-benchmark JSON -> markdown comparison tables
  data/                generated datasets (gitignored)
  runs/                result JSON + rendered reports (gitignored)
```

## 1. Generate datasets

The generator emits the exact format consumed by
`ContextTree.index_pageindex` / `DocumentTreeAdapter`
(`doc_name` / `doc_description` / recursive `structure` with
`node_id`, `title`, `summary`, `start_index`, `end_index`, `text`, `nodes`).
Content is random; only the tree *shape* matters.

```bash
python bench/db/gen_pageindex.py --scale small  --out bench/db/data/small.json   # ~5k nodes, 5 MB
python bench/db/gen_pageindex.py --scale medium --out bench/db/data/medium.json  # ~71k nodes, 85 MB
python bench/db/gen_pageindex.py --scale large  --out bench/db/data/large.json   # ~819k nodes, 1.15 GB
```

## 2. Start the databases (docker)

```bash
docker run -d --name condb_pg    -e POSTGRES_PASSWORD=bench -e POSTGRES_DB=bench -p 55432:5432 postgres:16
docker run -d --name condb_mongo -p 57017:27017 mongo:7
```

DuckDB and SQLite are embedded (no server).

## 3. Python env (isolated, via uv)

```bash
uv venv bench/db/.venv --python 3.10
uv pip install --python bench/db/.venv "pymongo>=4.6" "psycopg[binary]>=3.1" "duckdb>=0.10" "pyarrow>=15"
```

## 4. Run

```bash
./bench/db/.venv/bin/python bench/db/bench_databases.py \
    --doc bench/db/data/medium.json \
    --engines sqlite duckdb postgres mongo \
    --out bench/db/runs/medium.json

./bench/db/.venv/bin/python bench/db/report.py bench/db/runs/*.json --out bench/db/runs/RESULTS.md
```

## Schema & query model

Every engine stores **one record per node** with identical logical fields
(`tree_id, node_id, parent_id, depth, path, title, summary, start_index,
end_index, text`) and answers the **same six query semantics**:

| query | meaning | ConDB analogue |
|---|---|---|
| `q_point`    | node by `(tree_id, node_id)`            | `get_node` |
| `q_children` | direct children of a node               | `get_children` |
| `q_subtree`  | all descendants (materialized-path range) | `get_subtree` / `expand` |
| `q_depth`    | all nodes at a depth (metadata filter)  | level scan |
| `q_range`    | `start_index` window                     | page-neighborhood |
| `q_search`   | `summary` substring (unindexed scan)     | naive lexical filter |

Subtree descendants use a **materialized path** + range predicate
(`path >= P || '/'  AND  path < P || '0'`) so it is index-friendly and
identical across all four engines.

Engine-idiomatic storage:

- **MongoDB** - one BSON document per node, b-tree indexes. WiredTiger snappy
  compression is on by default, so its on-disk footprint is *compressed*; the
  result JSON also records the uncompressed logical size (`storage.uncompressed`).
- **PostgreSQL** - `JSONB` document column (`doc`) + scalar columns for the
  indexed keys, with a GIN index on `doc`. This is the documented
  "Postgres as a document store" model.
- **DuckDB** - typed columns (its native columnar layout), ART indexes on the
  point/range keys.
- **SQLite** - typed columns + b-tree indexes (ConDB's current storage engine).

## Notes on fairness

- Point lookups are scoped by `(tree_id, node_id)` - the real ConDB access
  pattern - so every engine can use its composite/primary index.
- `q_search` is deliberately **unindexed** (substring scan) to show full-scan
  behavior; it is not a full-text-index comparison.
- MongoDB `storageSize` is post-compression; Postgres/SQLite/DuckDB sizes are
  uncompressed on-disk. Compare totals with that in mind (see RESULTS.md).
