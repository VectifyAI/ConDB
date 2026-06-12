#!/usr/bin/env python3
"""Operator benchmark for ConDB retrieval: subtree traversal and content fetch.

Two operations the retrievers issue beyond plain point/children reads:

  descendants  get_subtree implemented by chasing parent_id with a recursive
               query (the alternative to the materialized-path range in
               bench_databases.py)
  join         fetch a node's children together with their content, i.e. node
               structure joined to entity content (get_children + get_entity)

The join uses a real two-relation split (a `nodes` table for structure and an
`entity` table for content), mirroring ConDB's Node/Entity storage, so it
exercises SQL JOIN vs MongoDB $lookup. Recursive traversal uses WITH RECURSIVE
on the SQL engines and $graphLookup on MongoDB.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

from bench_databases import flatten

NODE_COLS = ["tree_id", "node_id", "parent_id", "depth", "path", "title", "start_index", "end_index"]
ENTITY_COLS = ["tree_id", "node_id", "summary", "text"]


def percentiles(lat_ms):
    lat_ms = sorted(lat_ms)

    def pct(p):
        if not lat_ms:
            return 0.0
        idx = min(len(lat_ms) - 1, int(round(p / 100 * (len(lat_ms) - 1))))
        return round(lat_ms[idx], 3)

    return {"p50_ms": pct(50), "p95_ms": pct(95), "p99_ms": pct(99),
            "mean_ms": round(statistics.mean(lat_ms), 3) if lat_ms else 0.0}


def split_rows(rows):
    node_rows = [{c: r[c] for c in NODE_COLS} for r in rows]
    entity_rows = [{c: r[c] for c in ENTITY_COLS} for r in rows]
    return node_rows, entity_rows


class SqliteOps:
    name = "sqlite"

    def __init__(self, path):
        self.path = path

    def setup(self, node_rows, entity_rows):
        import sqlite3
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except FileNotFoundError:
                pass
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE nodes (tree_id TEXT, node_id TEXT, parent_id TEXT, "
                          "depth INTEGER, path TEXT, title TEXT, start_index INTEGER, end_index INTEGER)")
        self.conn.execute("CREATE TABLE entity (tree_id TEXT, node_id TEXT, summary TEXT, text TEXT)")
        self.conn.executemany(
            f"INSERT INTO nodes ({','.join(NODE_COLS)}) VALUES ({','.join('?' * len(NODE_COLS))})",
            [tuple(r[c] for c in NODE_COLS) for r in node_rows])
        self.conn.executemany(
            f"INSERT INTO entity ({','.join(ENTITY_COLS)}) VALUES ({','.join('?' * len(ENTITY_COLS))})",
            [tuple(r[c] for c in ENTITY_COLS) for r in entity_rows])
        self.conn.execute("CREATE UNIQUE INDEX ix_n ON nodes(tree_id, node_id)")
        self.conn.execute("CREATE INDEX ix_parent ON nodes(tree_id, parent_id)")
        self.conn.execute("CREATE UNIQUE INDEX ix_e ON entity(tree_id, node_id)")
        self.conn.commit()
        self.conn.execute("ANALYZE")

    def descendants(self, node_id):
        cur = self.conn.execute(
            "WITH RECURSIVE sub(node_id) AS ("
            "  SELECT node_id FROM nodes WHERE tree_id=? AND node_id=? "
            "  UNION ALL "
            "  SELECT n.node_id FROM nodes n JOIN sub ON n.parent_id=sub.node_id WHERE n.tree_id=?"
            ") SELECT count(*) - 1 FROM sub",
            (self.tree_id, node_id, self.tree_id))
        return cur.fetchone()[0]

    def join(self, parent_id):
        cur = self.conn.execute(
            "SELECT n.node_id, e.summary FROM nodes n JOIN entity e "
            "ON n.tree_id=e.tree_id AND n.node_id=e.node_id WHERE n.tree_id=? AND n.parent_id=?",
            (self.tree_id, parent_id))
        return len(cur.fetchall())

    def teardown(self):
        self.conn.close()


class DuckDBOps:
    name = "duckdb"

    def __init__(self, path):
        self.path = path

    def setup(self, node_rows, entity_rows):
        import duckdb
        import pyarrow as pa
        for ext in ("", ".wal"):
            try:
                os.remove(self.path + ext)
            except FileNotFoundError:
                pass
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(self.path)
        self.conn.execute("CREATE TABLE nodes (tree_id VARCHAR, node_id VARCHAR, parent_id VARCHAR, "
                          "depth INTEGER, path VARCHAR, title VARCHAR, start_index INTEGER, end_index INTEGER)")
        self.conn.execute("CREATE TABLE entity (tree_id VARCHAR, node_id VARCHAR, summary VARCHAR, text VARCHAR)")
        self.conn.register("sn", pa.table({c: [r[c] for r in node_rows] for c in NODE_COLS}))
        self.conn.execute(f"INSERT INTO nodes SELECT {','.join(NODE_COLS)} FROM sn")
        self.conn.unregister("sn")
        self.conn.register("se", pa.table({c: [r[c] for r in entity_rows] for c in ENTITY_COLS}))
        self.conn.execute(f"INSERT INTO entity SELECT {','.join(ENTITY_COLS)} FROM se")
        self.conn.unregister("se")
        self.conn.execute("CREATE INDEX ix_parent ON nodes(parent_id)")
        self.conn.execute("CREATE INDEX ix_ne ON entity(node_id)")

    def descendants(self, node_id):
        return self.conn.execute(
            "WITH RECURSIVE sub(node_id) AS ("
            "  SELECT node_id FROM nodes WHERE tree_id=? AND node_id=? "
            "  UNION ALL "
            "  SELECT n.node_id FROM nodes n JOIN sub ON n.parent_id=sub.node_id WHERE n.tree_id=?"
            ") SELECT count(*) - 1 FROM sub",
            [self.tree_id, node_id, self.tree_id]).fetchone()[0]

    def join(self, parent_id):
        return len(self.conn.execute(
            "SELECT n.node_id, e.summary FROM nodes n JOIN entity e "
            "ON n.tree_id=e.tree_id AND n.node_id=e.node_id WHERE n.tree_id=? AND n.parent_id=?",
            [self.tree_id, parent_id]).fetchall())

    def teardown(self):
        self.conn.close()


class PostgresOps:
    name = "postgres"

    def __init__(self, dsn):
        self.dsn = dsn

    def setup(self, node_rows, entity_rows):
        import psycopg
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.conn.execute("DROP TABLE IF EXISTS nodes")
        self.conn.execute("DROP TABLE IF EXISTS entity")
        self.conn.execute("CREATE TABLE nodes (tree_id TEXT, node_id TEXT, parent_id TEXT, "
                          "depth INT, path TEXT, title TEXT, start_index INT, end_index INT)")
        self.conn.execute("CREATE TABLE entity (tree_id TEXT, node_id TEXT, summary TEXT, text TEXT)")
        with self.conn.cursor() as cur:
            with cur.copy(f"COPY nodes ({','.join(NODE_COLS)}) FROM STDIN") as cp:
                for r in node_rows:
                    cp.write_row(tuple(r[c] for c in NODE_COLS))
            with cur.copy(f"COPY entity ({','.join(ENTITY_COLS)}) FROM STDIN") as cp:
                for r in entity_rows:
                    cp.write_row(tuple(r[c] for c in ENTITY_COLS))
        self.conn.execute("ALTER TABLE nodes ADD PRIMARY KEY (tree_id, node_id)")
        self.conn.execute("CREATE INDEX ix_parent ON nodes(parent_id)")
        self.conn.execute("ALTER TABLE entity ADD PRIMARY KEY (tree_id, node_id)")
        self.conn.execute("ANALYZE nodes")
        self.conn.execute("ANALYZE entity")

    def descendants(self, node_id):
        return self.conn.execute(
            "WITH RECURSIVE sub(node_id) AS ("
            "  SELECT node_id FROM nodes WHERE tree_id=%s AND node_id=%s "
            "  UNION ALL "
            "  SELECT n.node_id FROM nodes n JOIN sub ON n.parent_id=sub.node_id WHERE n.tree_id=%s"
            ") SELECT count(*) - 1 FROM sub",
            (self.tree_id, node_id, self.tree_id)).fetchone()[0]

    def join(self, parent_id):
        return len(self.conn.execute(
            "SELECT n.node_id, e.summary FROM nodes n JOIN entity e "
            "ON n.tree_id=e.tree_id AND n.node_id=e.node_id WHERE n.tree_id=%s AND n.parent_id=%s",
            (self.tree_id, parent_id)).fetchall())

    def teardown(self):
        self.conn.close()


class MongoOps:
    name = "mongo"

    def __init__(self, uri):
        self.uri = uri

    def setup(self, node_rows, entity_rows):
        from pymongo import ASCENDING, MongoClient
        self.client = MongoClient(self.uri)
        self.db = self.client["bench"]
        self.db.drop_collection("nodes")
        self.db.drop_collection("entity")
        self.nodes = self.db["nodes"]
        self.entity = self.db["entity"]
        for i in range(0, len(node_rows), 10000):
            self.nodes.insert_many([dict(r) for r in node_rows[i:i + 10000]], ordered=False)
        for i in range(0, len(entity_rows), 10000):
            self.entity.insert_many([dict(r) for r in entity_rows[i:i + 10000]], ordered=False)
        self.nodes.create_index([("tree_id", ASCENDING), ("node_id", ASCENDING)])
        self.nodes.create_index([("parent_id", ASCENDING)])  # $graphLookup connectToField
        self.entity.create_index([("node_id", ASCENDING)])  # $lookup foreignField

    def descendants(self, node_id):
        pipe = [
            {"$match": {"tree_id": self.tree_id, "node_id": node_id}},
            {"$graphLookup": {"from": "nodes", "startWith": "$node_id",
                              "connectFromField": "node_id", "connectToField": "parent_id",
                              "as": "sub"}},
            {"$project": {"n": {"$size": "$sub"}}},
        ]
        return next(iter(self.nodes.aggregate(pipe, allowDiskUse=True)), {}).get("n", 0)

    def join(self, parent_id):
        pipe = [
            {"$match": {"tree_id": self.tree_id, "parent_id": parent_id}},
            {"$lookup": {"from": "entity", "localField": "node_id",
                         "foreignField": "node_id", "as": "e"}},
            {"$project": {"node_id": 1, "summary": {"$first": "$e.summary"}}},
        ]
        return len(list(self.nodes.aggregate(pipe)))

    def teardown(self):
        self.client.close()


OPS = [
    ("descendants", "get_subtree (recursive parent_id)", "shallow"),
    ("join", "fetch children with content (node + entity)", "parents"),
]


def pick_samples(recs, kind, rng):
    if kind == "shallow":
        return [p.rsplit("/", 1)[-1] for p in recs.subtree_paths[:100]] if recs.subtree_paths else []
    if kind == "parents":
        return recs.parent_ids[:200]
    return []


def time_op(fn, samples, warmup=2):
    for s in samples[:warmup]:
        fn(s)
    lat, rows = [], 0
    for s in samples:
        t0 = time.perf_counter()
        r = fn(s)
        lat.append((time.perf_counter() - t0) * 1000.0)
        if isinstance(r, int):
            rows += r
    out = percentiles(lat)
    out["avg_rows"] = round(rows / len(lat), 1) if lat else 0.0
    return out


def build(name, args):
    if name == "sqlite":
        return SqliteOps(args.sqlite_path)
    if name == "duckdb":
        return DuckDBOps(args.duckdb_path)
    if name == "postgres":
        return PostgresOps(args.pg_dsn)
    if name == "mongo":
        return MongoOps(args.mongo_uri)
    raise ValueError(name)


def run_backend(be, recs, seed):
    rng = random.Random(seed)
    print(f"  [{be.name}] setup + ingest (2 relations)...", file=sys.stderr)
    be.tree_id = recs.rows[0]["tree_id"]
    node_rows, entity_rows = split_rows(recs.rows)
    be.setup(node_rows, entity_rows)
    result = {"engine": be.name, "ops": {}}
    for op, desc, kind in OPS:
        samples = pick_samples(recs, kind, rng)
        if not samples:
            continue
        print(f"  [{be.name}] {op} ({len(samples)} calls)...", file=sys.stderr)
        stats = time_op(getattr(be, op), samples)
        stats["desc"] = desc
        result["ops"][op] = stats
    be.teardown()
    return result


def main():
    ap = argparse.ArgumentParser(description="Operator benchmark (recursive subtree, content-fetch join).")
    ap.add_argument("--doc", required=True)
    ap.add_argument("--engines", nargs="+", default=["sqlite", "duckdb", "postgres", "mongo"])
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out")
    ap.add_argument("--pg-dsn", default="host=localhost port=55432 dbname=bench user=postgres password=bench")
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--sqlite-path", default="bench/db/runs/_sqlite_op.db")
    ap.add_argument("--duckdb-path", default="bench/db/runs/_duck_op.db")
    args = ap.parse_args()

    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id=Path(args.doc).stem)
    print(f"Loaded {len(recs.rows):,} nodes from {args.doc}", file=sys.stderr)

    results = {"doc": args.doc, "node_count": len(recs.rows), "engines": {}}
    for eng in args.engines:
        print(f"\n=== {eng} ===", file=sys.stderr)
        try:
            results["engines"][eng] = run_backend(build(eng, args), recs, args.seed)
        except Exception as e:
            import traceback
            traceback.print_exc()
            results["engines"][eng] = {"engine": eng, "error": str(e)}

    print(f"\n{'op':14s} | " + " | ".join(f"{e:>10s}" for e in results["engines"]))
    for op, _, _ in OPS:
        cells = []
        for e in results["engines"]:
            v = results["engines"][e].get("ops", {}).get(op, {})
            cells.append(f"{v.get('p50_ms', 0):10.3f}" if v else f"{'-':>10s}")
        print(f"{op:14s} | " + " | ".join(cells))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\nWrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
