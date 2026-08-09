#!/usr/bin/env python3
"""Matched three-configuration get_subtree comparison, one engine per invocation.

Separates the engine effect from the schema effect. All configurations use the same
host, same 200 seeded subtree paths, same id-only result set.

  naive     monolithic one-record-per-node layout, range scan on the plain
            path index (the plain-index baseline)
  covered   same layout, same query, served index-only by a covering index:
            sqlite  ix_cover(path, node_id)
            postgres (path) INCLUDE (node_id), VACUUM first so the
                     visibility map allows a true Index Only Scan
            mongo   {path, node_id} with _id excluded from the projection
            duckdb  (columnar projection already reads only node_id;
                     no separate covered result is measured)
  deployed  the decoupled ~150-byte structure table/collection
            (tree_id, node_id, parent_id, depth, path) under the same
            covering index (a candidate narrow-structure layout) — mongo, postgres
            and sqlite symmetrically; duckdb skipped because this benchmark
            does not define an equivalent narrow-layout intervention for its
            columnar storage path)

One engine runs per invocation. The client releases its row list after ingest,
so the measurement window holds only the engine's working set. Everything is
dropped on exit.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from bench_databases import (DuckDBBackend, MongoBackend, PostgresBackend,
                             SqliteBackend, flatten, time_calls)

STRUCT_FIELDS = ("tree_id", "node_id", "parent_id", "depth", "path")


def log(m):
    print(m, file=sys.stderr, flush=True)


def guard(floor=10.0):
    free = shutil.disk_usage("/").free / 1e9
    if free < floor:
        raise SystemExit(f"ABORT: free disk {free:.1f}GB < {floor}GB")


# ---- per-engine covered-arm plumbing ---------------------------------------

def sqlite_verify(be, table):
    plan = be.conn.execute(
        f"EXPLAIN QUERY PLAN SELECT node_id FROM {table} WHERE path>=? AND path<?",
        ("x/", "x0")).fetchall()
    return {"plan": " | ".join(str(r) for r in plan),
            "covered": "COVERING INDEX" in str(plan)}


def sqlite_cover(be):
    be.conn.execute("CREATE INDEX ix_cover ON nodes(path, node_id)")
    be.conn.execute("ANALYZE")
    return sqlite_verify(be, "nodes")


def postgres_verify(be, table):
    plan = [r[0] for r in be.conn.execute(
        f"EXPLAIN SELECT node_id FROM {table} WHERE path>=%s AND path<%s",
        ("x/", "x0")).fetchall()]
    return {"plan": " | ".join(plan),
            "covered": any("Index Only Scan" in ln for ln in plan)}


def postgres_cover(be):
    be.conn.execute("CREATE INDEX ix_cover ON nodes (path) INCLUDE (node_id)")
    log("      VACUUM ANALYZE nodes (visibility map for index-only scan)...")
    # Docker's default /dev/shm is 64 MiB.  Parallel VACUUM can exceed that
    # limit on the 3M-node relation even though the data volume has ample disk
    # space.  PARALLEL 0 changes only visibility-map preparation, not the
    # measured SELECT plan.
    be.conn.execute("VACUUM (ANALYZE, PARALLEL 0) nodes")
    return postgres_verify(be, "nodes")


def mongo_explain(col, projection, hint):
    e = col.find({"path": {"$gte": "x/", "$lt": "x0"}}, projection).hint(hint).explain()
    names = []

    def walk(s):
        if isinstance(s, dict):
            if "stage" in s:
                names.append(s["stage"])
            if "inputStage" in s:
                walk(s["inputStage"])
    walk(e.get("queryPlanner", {}).get("winningPlan", {}))
    return {"plan": " | ".join(names), "covered": "FETCH" not in names}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="data/large.json")
    ap.add_argument("--engine", required=True,
                    choices=["mongo", "postgres", "sqlite", "duckdb"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--pg-dsn", default="host=localhost port=55432 dbname=bench user=postgres password=bench")
    ap.add_argument("--sqlite-path", default="runs/_fair_sqlite.db")
    ap.add_argument("--duckdb-path", default="runs/_fair_duck.db")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    guard()
    log(f"loading {args.doc} ...")
    t0 = time.time()
    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id="base", seed=7)
    del doc
    paths = recs.subtree_paths
    log(f"flattened {len(recs.rows):,} nodes in {time.time()-t0:.0f}s; {len(paths)} paths")

    be = {"mongo": lambda: MongoBackend(args.mongo_uri),
          "postgres": lambda: PostgresBackend(args.pg_dsn),
          "sqlite": lambda: SqliteBackend(args.sqlite_path),
          "duckdb": lambda: DuckDBBackend(args.duckdb_path)}[args.engine]()
    be.tree_id = "base"

    out = {"doc": args.doc, "engine": args.engine, "nodes": len(recs.rows),
           "paths": len(paths), "arms": {}}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    def save():
        Path(args.out).write_text(json.dumps(out, indent=2))

    struct = None
    try:
        log(f"[{args.engine}] ingest monolithic layout ...")
        be.setup()
        ing_s = be.ingest(recs.rows)
        idx_s = be.build_indexes()
        out["ingest_s"], out["index_s"] = round(ing_s, 1), round(idx_s, 1)
        out["storage"] = be.storage_bytes()
        log(f"  ingested in {ing_s:.0f}s, indexes {idx_s:.0f}s")

        if args.engine == "mongo":
            log("[mongo] ingest deployed structure collection ...")
            t0 = time.time()
            from pymongo import ASCENDING
            be.db.drop_collection("fair_struct")
            struct = be.db["fair_struct"]
            buf = []
            for r in recs.rows:
                buf.append({k: r[k] for k in STRUCT_FIELDS})
                if len(buf) >= 10000:
                    struct.insert_many(buf, ordered=False); buf = []
            if buf:
                struct.insert_many(buf, ordered=False)
            struct.create_index([("path", ASCENDING), ("node_id", ASCENDING)])
            out["struct_build_s"] = round(time.time() - t0, 1)
        elif args.engine == "postgres":
            log("[postgres] ingest deployed structure table ...")
            t0 = time.time()
            be.conn.execute("DROP TABLE IF EXISTS fair_struct")
            be.conn.execute("""
                CREATE TABLE fair_struct (
                    tree_id TEXT, node_id TEXT, parent_id TEXT,
                    depth INT, path TEXT COLLATE "C"
                )""")
            with be.conn.cursor() as cur:
                with cur.copy("COPY fair_struct (tree_id,node_id,parent_id,depth,path) FROM STDIN") as cp:
                    for r in recs.rows:
                        cp.write_row(tuple(r[k] for k in STRUCT_FIELDS))
            be.conn.execute(
                "CREATE INDEX ix_struct_cover ON fair_struct (path) INCLUDE (node_id)")
            log("      VACUUM ANALYZE fair_struct (visibility map for index-only scan)...")
            be.conn.execute("VACUUM (ANALYZE, PARALLEL 0) fair_struct")
            out["struct_build_s"] = round(time.time() - t0, 1)
        elif args.engine == "sqlite":
            log("[sqlite] ingest deployed structure table ...")
            t0 = time.time()
            be.conn.execute("""
                CREATE TABLE fair_struct (
                    tree_id TEXT, node_id TEXT, parent_id TEXT,
                    depth INTEGER, path TEXT
                )""")
            be.conn.executemany(
                "INSERT INTO fair_struct (tree_id,node_id,parent_id,depth,path) VALUES (?,?,?,?,?)",
                (tuple(r[k] for k in STRUCT_FIELDS) for r in recs.rows))
            be.conn.execute("CREATE INDEX ix_struct_cover ON fair_struct(path, node_id)")
            be.conn.commit()
            be.conn.execute("ANALYZE")
            out["struct_build_s"] = round(time.time() - t0, 1)

        # rows are only needed for ingest; free them before measuring
        recs.rows.clear()

        # ---- arm 1: naive (plain path index, id-only) -----------------------
        if args.engine == "mongo":
            def q_naive(p):
                return len(list(be.col.find(
                    {"path": {"$gte": p + "/", "$lt": p + "0"}},
                    {"node_id": 1, "_id": 0}).hint([("path", 1)])))
        else:
            q_naive = be.q_subtree
        log("arm 1: naive ...")
        out["arms"]["naive"] = time_calls(q_naive, paths)
        save()

        # ---- arm 2: covered (same layout, covering index) -------------------
        if args.engine == "duckdb":
            out["arms"]["covered"] = {
                "note": "not measured; columnar projection already reads only node_id"}
        else:
            log("arm 2: build covering index ...")
            t0 = time.time()
            if args.engine == "sqlite":
                ver = sqlite_cover(be)
                q_cov = be.q_subtree
            elif args.engine == "postgres":
                ver = postgres_cover(be)
                q_cov = be.q_subtree
            else:
                from pymongo import ASCENDING
                be.col.create_index([("path", ASCENDING), ("node_id", ASCENDING)])
                ver = mongo_explain(be.col, {"node_id": 1, "_id": 0},
                                    [("path", 1), ("node_id", 1)])

                def q_cov(p):
                    return len(list(be.col.find(
                        {"path": {"$gte": p + "/", "$lt": p + "0"}},
                        {"node_id": 1, "_id": 0}).hint([("path", 1), ("node_id", 1)])))
            log(f"  built in {time.time()-t0:.0f}s; covered={ver['covered']}")
            log("arm 2: covered ...")
            out["arms"]["covered"] = {"verify": ver, **time_calls(q_cov, paths)}
            save()

        # ---- arm 3: deployed (decoupled structure table/collection) ---------
        if args.engine == "duckdb":
            out["arms"]["deployed"] = {
                "note": "not measured; no equivalent narrow-layout intervention is defined"}
            save()
        else:
            if args.engine == "mongo":
                ver3 = mongo_explain(struct, {"node_id": 1, "_id": 0},
                                     [("path", 1), ("node_id", 1)])

                def q_dep(p):
                    return len(list(struct.find(
                        {"path": {"$gte": p + "/", "$lt": p + "0"}},
                        {"node_id": 1, "_id": 0}).hint([("path", 1), ("node_id", 1)])))
            elif args.engine == "postgres":
                ver3 = postgres_verify(be, "fair_struct")

                def q_dep(p):
                    return len(be.conn.execute(
                        "SELECT node_id FROM fair_struct WHERE path>=%s AND path<%s",
                        (p + "/", p + "0")).fetchall())
            else:  # sqlite
                ver3 = sqlite_verify(be, "fair_struct")

                def q_dep(p):
                    return len(be.conn.execute(
                        "SELECT node_id FROM fair_struct WHERE path>=? AND path<?",
                        (p + "/", p + "0")).fetchall())
            log("arm 3: deployed structure table/collection ...")
            out["arms"]["deployed"] = {"verify": ver3, **time_calls(q_dep, paths)}
            save()
    finally:
        if not args.keep:
            try:
                if args.engine == "mongo":
                    be.db.drop_collection("nodes")
                    be.db.drop_collection("fair_struct")
                elif args.engine == "postgres":
                    be.conn.execute("DROP TABLE IF EXISTS nodes")
                    be.conn.execute("DROP TABLE IF EXISTS fair_struct")
                elif args.engine == "sqlite":
                    be.teardown()
                    for ext in ("", "-wal", "-shm"):
                        try: os.remove(args.sqlite_path + ext)
                        except FileNotFoundError: pass
                elif args.engine == "duckdb":
                    be.teardown()
                    for ext in ("", ".wal"):
                        try: os.remove(args.duckdb_path + ext)
                        except FileNotFoundError: pass
                log("cleaned up")
            except Exception as e:  # noqa: BLE001
                log(f"cleanup: {e!r}")
        save()

    def line(tag, st):
        if not st or "p50_ms" not in st:
            return f"  {tag:10s} ({st.get('note', 'n/a') if st else 'n/a'})"
        return (f"  {tag:10s} p50={st['p50_ms']:8.2f}  p95={st['p95_ms']:9.2f}  "
                f"p99={st['p99_ms']:9.2f}  rows~{st['avg_rows']:.0f}")

    print(f"\n===== fair get_subtree, {args.engine}, {out['nodes']:,} nodes =====")
    for arm in ("naive", "covered", "deployed"):
        if arm in out["arms"]:
            print(line(arm, out["arms"][arm]))
    for arm in ("covered", "deployed"):
        v = out["arms"].get(arm, {}).get("verify")
        if v:
            print(f"  {arm} plan: {v['plan']}  covered={v['covered']}")
    log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
