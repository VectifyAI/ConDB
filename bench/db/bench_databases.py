#!/usr/bin/env python3
"""Compare databases on the read operations ConDB's retrievers issue.

Flattens a PageIndex tree into one record per node, ingests it into each engine
(MongoDB, PostgreSQL/JSONB, DuckDB, SQLite) under the same logical schema, and
measures storage, ingest, and the three retrieval reads the retrievers use:

    get_node      point lookup by (tree_id, node_id)
    get_children  a node's direct children
    get_subtree   a node's subtree via a materialized-path range
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

COLS = ["tree_id", "node_id", "parent_id", "depth", "path",
        "title", "summary", "start_index", "end_index", "text"]


@dataclass
class Records:
    rows: list[dict]
    point_ids: list[str]
    parent_ids: list[str]
    subtree_paths: list[str]


def flatten(doc, tree_id, seed=7):
    rng = random.Random(seed)
    rows = []
    internal_paths = []
    internal_ids = []

    def walk(node, parent_id, parent_path, depth):
        nid = node["node_id"]
        path = f"{parent_path}/{nid}"
        children = node.get("nodes")
        rows.append({
            "tree_id": tree_id,
            "node_id": nid,
            "parent_id": parent_id,
            "depth": depth,
            "path": path,
            "title": node.get("title", ""),
            "summary": node.get("summary", ""),
            "start_index": node.get("start_index", 0),
            "end_index": node.get("end_index", 0),
            "text": node.get("text", "") or "",
        })
        if children:
            internal_ids.append(nid)
            internal_paths.append(path)
            for c in children:
                walk(c, nid, path, depth + 1)

    for top in doc.get("structure", []):
        walk(top, None, "", 1)

    n = len(rows)
    point_ids = [rows[rng.randrange(n)]["node_id"] for _ in range(min(500, n))]
    parent_ids = rng.sample(internal_ids, min(500, len(internal_ids))) if internal_ids else []
    shallow = [p for p in internal_paths if p.count("/") <= 3] or internal_paths
    subtree_paths = rng.sample(shallow, min(200, len(shallow))) if shallow else []
    return Records(rows, point_ids, parent_ids, subtree_paths)


def time_calls(fn, items, warmup=3):
    items = list(items)
    for w in items[:warmup]:
        fn(w)
    lat_ms = []
    rows_returned = 0
    for it in items:
        t0 = time.perf_counter()
        res = fn(it)
        lat_ms.append((time.perf_counter() - t0) * 1000.0)
        if isinstance(res, int):
            rows_returned += res
    lat_ms.sort()

    def pct(p):
        if not lat_ms:
            return 0.0
        idx = min(len(lat_ms) - 1, int(round(p / 100 * (len(lat_ms) - 1))))
        return round(lat_ms[idx], 3)

    mean = statistics.mean(lat_ms) if lat_ms else 0.0
    return {
        "n": len(lat_ms),
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "p99_ms": pct(99),
        "mean_ms": round(mean, 3),
        "avg_rows": round(rows_returned / len(lat_ms), 1) if lat_ms else 0.0,
    }


def human(nbytes):
    if nbytes < 1e6:
        return f"{nbytes / 1e3:.1f} KB"
    if nbytes < 1e9:
        return f"{nbytes / 1e6:.1f} MB"
    return f"{nbytes / 1e9:.2f} GB"


class SqliteBackend:
    name = "sqlite"
    tree_id = ""

    def __init__(self, path):
        self.path = path

    def setup(self):
        import sqlite3
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except FileNotFoundError:
                pass
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("""
            CREATE TABLE nodes (
                tree_id TEXT, node_id TEXT, parent_id TEXT, depth INTEGER,
                path TEXT, title TEXT, summary TEXT,
                start_index INTEGER, end_index INTEGER, text TEXT
            )""")

    def ingest(self, rows):
        t0 = time.time()
        self.conn.executemany(
            f"INSERT INTO nodes ({','.join(COLS)}) VALUES ({','.join(['?'] * len(COLS))})",
            [tuple(r[c] for c in COLS) for r in rows],
        )
        self.conn.commit()
        return time.time() - t0

    def build_indexes(self):
        t0 = time.time()
        self.conn.execute("CREATE UNIQUE INDEX ix_pk ON nodes(tree_id, node_id)")
        self.conn.execute("CREATE INDEX ix_parent ON nodes(parent_id)")
        self.conn.execute("CREATE INDEX ix_path ON nodes(path)")
        self.conn.commit()
        self.conn.execute("ANALYZE")
        return time.time() - t0

    def storage_bytes(self):
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        total = sum(os.path.getsize(self.path + ext) for ext in ("", "-wal", "-shm")
                    if os.path.exists(self.path + ext))
        idx = self.conn.execute(
            "SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name LIKE 'ix_%'").fetchone()[0]
        return {"data": total - idx, "index": idx, "total": total}

    def q_point(self, node_id):
        c = self.conn.execute(
            "SELECT title,summary,start_index,end_index FROM nodes WHERE tree_id=? AND node_id=?",
            (self.tree_id, node_id))
        return len(c.fetchall())

    def q_children(self, parent_id):
        return len(self.conn.execute("SELECT node_id,title FROM nodes WHERE parent_id=?", (parent_id,)).fetchall())

    def q_subtree(self, path):
        return len(self.conn.execute(
            "SELECT node_id FROM nodes WHERE path>=? AND path<?", (path + "/", path + "0")).fetchall())

    def q_entity(self, node_id):
        return len(self.conn.execute(
            "SELECT text FROM nodes WHERE tree_id=? AND node_id=?", (self.tree_id, node_id)).fetchall())

    def teardown(self):
        self.conn.close()


class DuckDBBackend:
    name = "duckdb"
    tree_id = ""

    def __init__(self, path):
        self.path = path

    def setup(self):
        import duckdb
        for ext in ("", ".wal"):
            try:
                os.remove(self.path + ext)
            except FileNotFoundError:
                pass
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(self.path)
        self.conn.execute("""
            CREATE TABLE nodes (
                tree_id VARCHAR, node_id VARCHAR, parent_id VARCHAR, depth INTEGER,
                path VARCHAR, title VARCHAR, summary VARCHAR,
                start_index INTEGER, end_index INTEGER, text VARCHAR
            )""")

    def ingest(self, rows):
        import pyarrow as pa
        cols = {c: [r[c] for r in rows] for c in COLS}
        tbl = pa.table(cols)
        t0 = time.time()
        self.conn.register("staging", tbl)
        self.conn.execute(f"INSERT INTO nodes SELECT {','.join(COLS)} FROM staging")
        self.conn.unregister("staging")
        return time.time() - t0

    def build_indexes(self):
        t0 = time.time()
        self.conn.execute("CREATE INDEX ix_node ON nodes(node_id)")
        self.conn.execute("CREATE INDEX ix_parent ON nodes(parent_id)")
        self.conn.execute("CREATE INDEX ix_path ON nodes(path)")
        self.conn.execute("CHECKPOINT")
        return time.time() - t0

    def storage_bytes(self):
        self.conn.execute("CHECKPOINT")
        total = sum(os.path.getsize(self.path + ext) for ext in ("", ".wal")
                    if os.path.exists(self.path + ext))
        return {"data": total, "index": 0, "total": total}

    def q_point(self, node_id):
        return len(self.conn.execute(
            "SELECT title,summary,start_index,end_index FROM nodes WHERE tree_id=? AND node_id=?",
            [self.tree_id, node_id]).fetchall())

    def q_children(self, parent_id):
        return len(self.conn.execute("SELECT node_id,title FROM nodes WHERE parent_id=?", [parent_id]).fetchall())

    def q_subtree(self, path):
        return len(self.conn.execute(
            "SELECT node_id FROM nodes WHERE path>=? AND path<?", [path + "/", path + "0"]).fetchall())

    def q_entity(self, node_id):
        return len(self.conn.execute(
            "SELECT text FROM nodes WHERE tree_id=? AND node_id=?", [self.tree_id, node_id]).fetchall())

    def teardown(self):
        self.conn.close()


class PostgresBackend:
    name = "postgres"
    tree_id = ""

    def __init__(self, dsn):
        self.dsn = dsn

    def setup(self):
        import psycopg
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.conn.execute("DROP TABLE IF EXISTS nodes")
        # path uses C (byte-order) collation so the subtree range predicate
        # path >= P||'/' AND path < P||'0' behaves like the other engines'
        # binary comparison; the default en_US collation treats '/' as
        # ignorable and silently returns no rows.
        self.conn.execute("""
            CREATE TABLE nodes (
                tree_id TEXT, node_id TEXT, parent_id TEXT,
                depth INT, path TEXT COLLATE "C", start_index INT,
                doc JSONB
            )""")

    def ingest(self, rows):
        t0 = time.time()
        with self.conn.cursor() as cur:
            with cur.copy("COPY nodes (tree_id,node_id,parent_id,depth,path,start_index,doc) FROM STDIN") as cp:
                for r in rows:
                    doc = {"title": r["title"], "summary": r["summary"],
                           "start_index": r["start_index"], "end_index": r["end_index"],
                           "text": r["text"]}
                    cp.write_row((r["tree_id"], r["node_id"], r["parent_id"],
                                  r["depth"], r["path"], r["start_index"],
                                  json.dumps(doc, ensure_ascii=False)))
        return time.time() - t0

    def build_indexes(self):
        t0 = time.time()
        self.conn.execute("ALTER TABLE nodes ADD PRIMARY KEY (tree_id, node_id)")
        self.conn.execute("CREATE INDEX ix_parent ON nodes(parent_id)")
        self.conn.execute("CREATE INDEX ix_path ON nodes(path)")
        self.conn.execute("ANALYZE nodes")
        return time.time() - t0

    def storage_bytes(self):
        total = self.conn.execute("SELECT pg_total_relation_size('nodes')").fetchone()[0]
        idx = self.conn.execute("SELECT pg_indexes_size('nodes')").fetchone()[0]
        return {"data": total - idx, "index": idx, "total": total}

    def q_point(self, node_id):
        return len(self.conn.execute(
            "SELECT doc->>'title', doc->>'summary' FROM nodes WHERE tree_id=%s AND node_id=%s",
            (self.tree_id, node_id)).fetchall())

    def q_children(self, parent_id):
        return len(self.conn.execute(
            "SELECT node_id, doc->>'title' FROM nodes WHERE parent_id=%s", (parent_id,)).fetchall())

    def q_subtree(self, path):
        return len(self.conn.execute(
            "SELECT node_id FROM nodes WHERE path>=%s AND path<%s", (path + "/", path + "0")).fetchall())

    def q_entity(self, node_id):
        return len(self.conn.execute(
            "SELECT doc->>'text' FROM nodes WHERE tree_id=%s AND node_id=%s",
            (self.tree_id, node_id)).fetchall())

    def teardown(self):
        self.conn.close()


class MongoBackend:
    name = "mongo"
    tree_id = ""

    def __init__(self, uri):
        self.uri = uri

    def setup(self):
        from pymongo import MongoClient
        self.client = MongoClient(self.uri)
        self.db = self.client["bench"]
        self.db.drop_collection("nodes")
        self.col = self.db["nodes"]

    def ingest(self, rows):
        t0 = time.time()
        batch = []
        for r in rows:
            batch.append(dict(r))
            if len(batch) >= 10000:
                self.col.insert_many(batch, ordered=False)
                batch = []
        if batch:
            self.col.insert_many(batch, ordered=False)
        return time.time() - t0

    def build_indexes(self):
        from pymongo import ASCENDING
        t0 = time.time()
        self.col.create_index([("tree_id", ASCENDING), ("node_id", ASCENDING)])
        self.col.create_index([("parent_id", ASCENDING)])
        self.col.create_index([("path", ASCENDING)])
        return time.time() - t0

    def storage_bytes(self):
        st = self.db.command("collstats", "nodes")
        data = st.get("storageSize", 0)
        idx = st.get("totalIndexSize", 0)
        logical = st.get("size", 0)
        return {"data": data, "index": idx, "total": data + idx, "uncompressed": logical}

    def q_point(self, node_id):
        return len(list(self.col.find(
            {"tree_id": self.tree_id, "node_id": node_id},
            {"title": 1, "summary": 1, "start_index": 1, "end_index": 1})))

    def q_children(self, parent_id):
        return len(list(self.col.find({"parent_id": parent_id}, {"node_id": 1, "title": 1})))

    def q_subtree(self, path):
        return len(list(self.col.find({"path": {"$gte": path + "/", "$lt": path + "0"}}, {"node_id": 1})))

    def q_entity(self, node_id):
        return len(list(self.col.find(
            {"tree_id": self.tree_id, "node_id": node_id}, {"text": 1})))

    def teardown(self):
        self.client.close()


QUERIES = [
    ("q_point", "get_node", lambda r: r.point_ids),
    ("q_children", "get_children", lambda r: r.parent_ids),
    ("q_subtree", "get_subtree (path range)", lambda r: r.subtree_paths),
    ("q_entity", "get_entity (content fetch)", lambda r: r.point_ids),
]


def run_backend(be, recs):
    print(f"  [{be.name}] setup...", file=sys.stderr)
    be.tree_id = recs.rows[0]["tree_id"] if recs.rows else ""
    be.setup()
    print(f"  [{be.name}] ingest {len(recs.rows):,} rows...", file=sys.stderr)
    ingest_s = be.ingest(recs.rows)
    print(f"  [{be.name}] build indexes...", file=sys.stderr)
    index_s = be.build_indexes()
    storage = be.storage_bytes()
    n = len(recs.rows)
    result = {
        "engine": be.name,
        "ingest_seconds": round(ingest_s, 2),
        "ingest_nodes_per_sec": int(n / ingest_s) if ingest_s > 0 else 0,
        "index_build_seconds": round(index_s, 2),
        "storage": dict(storage),
        "storage_human": {k: human(v) for k, v in storage.items()},
        "bytes_per_node": round(storage["total"] / n, 1) if n else 0,
        "queries": {},
    }
    for qname, desc, picker in QUERIES:
        fn = getattr(be, qname)
        samples = picker(recs)
        if not samples:
            continue
        print(f"  [{be.name}] {qname} ({len(samples)} calls)...", file=sys.stderr)
        stats = time_calls(fn, samples)
        stats["desc"] = desc
        result["queries"][qname] = stats
    be.teardown()
    return result


def build_backend(name, args):
    if name == "sqlite":
        return SqliteBackend(args.sqlite_path)
    if name == "duckdb":
        return DuckDBBackend(args.duckdb_path)
    if name == "postgres":
        return PostgresBackend(args.pg_dsn)
    if name == "mongo":
        return MongoBackend(args.mongo_uri)
    raise ValueError(f"unknown engine: {name}")


def main():
    ap = argparse.ArgumentParser(description="Compare databases on PageIndex retrieval reads.")
    ap.add_argument("--doc", required=True, help="PageIndex JSON to benchmark")
    ap.add_argument("--engines", nargs="+", default=["sqlite", "duckdb", "postgres", "mongo"])
    ap.add_argument("--out", help="write JSON results here")
    ap.add_argument("--pg-dsn", default="host=localhost port=55432 dbname=bench user=postgres password=bench")
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--sqlite-path", default="bench/db/runs/_sqlite.db")
    ap.add_argument("--duckdb-path", default="bench/db/runs/_duck.db")
    args = ap.parse_args()

    print(f"Loading {args.doc} ...", file=sys.stderr)
    t0 = time.time()
    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id=Path(args.doc).stem)
    print(f"Flattened {len(recs.rows):,} nodes in {time.time() - t0:.1f}s", file=sys.stderr)

    results = {"doc": args.doc, "node_count": len(recs.rows), "engines": {}}
    for eng in args.engines:
        print(f"\n=== {eng} ===", file=sys.stderr)
        try:
            results["engines"][eng] = run_backend(build_backend(eng, args), recs)
        except Exception as e:
            import traceback
            traceback.print_exc()
            results["engines"][eng] = {"engine": eng, "error": str(e)}

    out = json.dumps(results, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out)
        print(f"\nWrote {args.out}", file=sys.stderr)
    print(out)


if __name__ == "__main__":
    main()
