#!/usr/bin/env python3
"""Concurrency benchmark: throughput and tail latency vs client count.

The read benchmark issues one query at a time, so its qps is 1/latency. That
understates the client-server engines (MongoDB, PostgreSQL), which amortize
per-request overhead across concurrent clients. This drives a point-lookup
workload from a growing pool of independent OS processes (not threads, so there
is no GIL bottleneck) and reports aggregate throughput and P99 at each level.

Each worker process opens its own connection. For SQLite and DuckDB the workers
open the database file read-only, so this also exercises true multi-process
read concurrency on the embedded engines.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from bench_databases import build_backend, flatten

MAX_CONCURRENT_WORKERS = 16


def worker(payload):
    engine = payload["engine"]
    params = payload["params"]
    tree_id = payload["tree_id"]
    ids = payload["ids"]
    duration = payload["duration"]
    seed = payload["seed"]

    if engine == "sqlite":
        import sqlite3
        conn = sqlite3.connect(params["sqlite_path"])
        conn.execute("PRAGMA query_only=ON")
        sql = "SELECT title,summary FROM nodes WHERE tree_id=? AND node_id=?"

        def query(nid):
            return conn.execute(sql, (tree_id, nid)).fetchall()

        close = conn.close
    elif engine == "duckdb":
        import duckdb
        conn = duckdb.connect(params["duckdb_path"], read_only=True)
        sql = "SELECT title,summary FROM nodes WHERE tree_id=? AND node_id=?"

        def query(nid):
            return conn.execute(sql, [tree_id, nid]).fetchall()

        close = conn.close
    elif engine == "postgres":
        import psycopg
        conn = psycopg.connect(params["pg_dsn"])
        sql = "SELECT doc->>'title', doc->>'summary' FROM nodes WHERE tree_id=%s AND node_id=%s"

        def query(nid):
            return conn.execute(sql, (tree_id, nid)).fetchall()

        close = conn.close
    elif engine == "mongo":
        from pymongo import MongoClient
        client = MongoClient(params["mongo_uri"])
        col = client["bench"]["nodes"]
        proj = {"title": 1, "summary": 1}

        def query(nid):
            return list(col.find({"tree_id": tree_id, "node_id": nid}, proj))

        close = client.close
    else:
        raise ValueError(engine)

    rng = random.Random(seed)
    for _ in range(5):
        query(ids[rng.randrange(len(ids))])

    lat = []
    count = 0
    t0 = time.time()
    end = t0 + duration
    while time.time() < end:
        t = time.perf_counter()
        query(ids[rng.randrange(len(ids))])
        lat.append((time.perf_counter() - t) * 1000.0)
        count += 1
    elapsed = time.time() - t0
    close()

    if len(lat) > 4000:  # cap returned sample so the pickle stays small
        lat = lat[:: len(lat) // 4000]
    return {"count": count, "elapsed": elapsed, "lat": lat}


def run_level(engine, params, tree_id, ids, concurrency, duration, seed):
    if not 1 <= concurrency <= MAX_CONCURRENT_WORKERS:
        raise ValueError(
            f"concurrency must be between 1 and {MAX_CONCURRENT_WORKERS}"
        )
    payloads = [{"engine": engine, "params": params, "tree_id": tree_id, "ids": ids,
                 "duration": duration, "seed": seed + i} for i in range(concurrency)]
    with ProcessPoolExecutor(max_workers=concurrency) as ex:
        results = list(ex.map(worker, payloads))

    total = sum(r["count"] for r in results)
    elapsed = max(r["elapsed"] for r in results)
    all_lat = sorted(x for r in results for x in r["lat"])

    def pct(p):
        if not all_lat:
            return 0.0
        return round(all_lat[min(len(all_lat) - 1, int(round(p / 100 * (len(all_lat) - 1))))], 3)

    return {
        "concurrency": concurrency,
        "throughput_ops_s": int(total / elapsed) if elapsed > 0 else 0,
        "p50_ms": pct(50),
        "p99_ms": pct(99),
        "mean_ms": round(statistics.mean(all_lat), 3) if all_lat else 0.0,
        "ops": total,
    }


def run_backend(engine, args, recs):
    print(f"  [{engine}] setup + ingest...", file=sys.stderr)
    be = build_backend(engine, args)
    be.tree_id = recs.rows[0]["tree_id"]
    be.setup()
    be.ingest(recs.rows)
    be.build_indexes()
    be.teardown()

    params = {"sqlite_path": args.sqlite_path, "duckdb_path": args.duckdb_path,
              "pg_dsn": args.pg_dsn, "mongo_uri": args.mongo_uri}
    result = {"engine": engine, "levels": []}
    for c in args.concurrency:
        print(f"  [{engine}] concurrency={c} for {args.duration}s...", file=sys.stderr)
        lvl = run_level(engine, params, recs.rows[0]["tree_id"], recs.point_ids,
                        c, args.duration, args.seed)
        result["levels"].append(lvl)
    return result


def main():
    ap = argparse.ArgumentParser(description="Concurrency benchmark (point-lookup QPS vs processes).")
    ap.add_argument("--doc", required=True)
    ap.add_argument("--engines", nargs="+", default=["sqlite", "duckdb", "postgres", "mongo"])
    ap.add_argument(
        "--concurrency",
        nargs="+",
        type=int,
        default=[1, 2, 4, 8, 16],
        help=f"concurrent client processes (maximum {MAX_CONCURRENT_WORKERS})",
    )
    ap.add_argument("--duration", type=float, default=5.0, help="seconds per concurrency level")
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--out")
    ap.add_argument("--pg-dsn", default="host=localhost port=55432 dbname=bench user=postgres password=bench")
    ap.add_argument("--mongo-uri", default="mongodb://localhost:57017")
    ap.add_argument("--sqlite-path", default="bench/db/runs/_sqlite_c.db")
    ap.add_argument("--duckdb-path", default="bench/db/runs/_duck_c.db")
    args = ap.parse_args()
    if not args.concurrency or any(
        level < 1 or level > MAX_CONCURRENT_WORKERS
        for level in args.concurrency
    ):
        raise SystemExit(
            f"concurrency levels must be between 1 and "
            f"{MAX_CONCURRENT_WORKERS}"
        )

    doc = json.load(open(args.doc))
    recs = flatten(doc, tree_id=Path(args.doc).stem)
    print(f"Loaded {len(recs.rows):,} nodes from {args.doc}", file=sys.stderr)

    results = {"doc": args.doc, "node_count": len(recs.rows),
               "concurrency_levels": args.concurrency, "engines": {}}
    for eng in args.engines:
        print(f"\n=== {eng} ===", file=sys.stderr)
        try:
            results["engines"][eng] = run_backend(eng, args, recs)
        except Exception as e:
            import traceback
            traceback.print_exc()
            results["engines"][eng] = {"engine": eng, "error": str(e)}

    print("\nthroughput (ops/s) by concurrency:")
    print(f"{'engine':10s} | " + " | ".join(f"c={c:<4d}" for c in args.concurrency))
    for eng, r in results["engines"].items():
        if "error" in r:
            print(f"{eng:10s} | ERROR")
            continue
        print(f"{eng:10s} | " + " | ".join(f"{lvl['throughput_ops_s']:6,}" for lvl in r["levels"]))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\nWrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
